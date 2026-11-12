use anyhow::{anyhow, bail, Result};
use base64::Engine;
use serde::Deserialize;
use std::io::Read;

#[derive(Debug, Deserialize, Default, Clone, Copy)]
pub struct Usage {
    #[serde(default)]
    pub input_tokens: u64,
    #[serde(default)]
    pub output_tokens: u64,
}

pub struct Generated {
    pub png: Vec<u8>,
    pub usage: Usage,
}

/// Cost in USD; prices are per 1M tokens.
pub fn cost_usd(usage: &Usage, price_in: f64, price_out: f64) -> f64 {
    (usage.input_tokens as f64 * price_in + usage.output_tokens as f64 * price_out) / 1_000_000.0
}

/// Parse an images API response body into image bytes + usage.
pub fn parse_response(body: &str) -> Result<Generated> {
    #[derive(Deserialize)]
    struct Item {
        b64_json: Option<String>,
    }
    #[derive(Deserialize)]
    struct Resp {
        #[serde(default)]
        data: Vec<Item>,
        #[serde(default)]
        usage: Usage,
    }
    let resp: Resp = serde_json::from_str(body).map_err(|e| anyhow!("bad OpenAI response: {e}"))?;
    let b64 = resp
        .data
        .into_iter()
        .next()
        .and_then(|i| i.b64_json)
        .ok_or_else(|| anyhow!("OpenAI response has no image"))?;
    let png = base64::engine::general_purpose::STANDARD.decode(b64.trim())?;
    Ok(Generated { png, usage: resp.usage })
}

pub fn sniff_mime(data: &[u8]) -> (&'static str, &'static str) {
    if data.starts_with(&[0x89, b'P', b'N', b'G']) {
        ("image/png", "png")
    } else if data.len() > 12 && &data[0..4] == b"RIFF" && &data[8..12] == b"WEBP" {
        ("image/webp", "webp")
    } else {
        ("image/jpeg", "jpg")
    }
}

/// Build a multipart/form-data body: text fields, then file parts `(field, filename, mime, bytes)`.
pub fn multipart(boundary: &str, fields: &[(&str, &str)], files: &[(&str, String, &str, &[u8])]) -> Vec<u8> {
    let mut out = Vec::new();
    for (k, v) in fields {
        out.extend(format!("--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").into_bytes());
    }
    for (k, name, mime, data) in files {
        out.extend(
            format!("--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{name}\"\r\nContent-Type: {mime}\r\n\r\n")
                .into_bytes(),
        );
        out.extend_from_slice(data);
        out.extend(b"\r\n");
    }
    out.extend(format!("--{boundary}--\r\n").into_bytes());
    out
}

pub fn boundary() -> String {
    let mut b = [0u8; 12];
    if let Ok(mut f) = std::fs::File::open("/dev/urandom") {
        let _ = f.read_exact(&mut b);
    }
    let hex: String = b.iter().map(|x| format!("{x:02x}")).collect();
    format!("----tirephoto{hex}")
}

/// Non-2xx answer from OpenAI.
#[derive(Debug)]
pub struct OpenAiHttp(pub u16, pub String);
impl std::fmt::Display for OpenAiHttp {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "OpenAI HTTP {}: {}", self.0, self.1)
    }
}
impl std::error::Error for OpenAiHttp {}

/// True when every next job would fail the same way (bad key, region closed, no quota or money, the proxy down):
/// stop instead of using up the attempts. None of these is paid for, so the job goes back to the queue.
pub fn is_fatal(e: &anyhow::Error) -> bool {
    match e.downcast_ref::<OpenAiHttp>() {
        Some(OpenAiHttp(401 | 403 | 429, _)) => true,
        Some(OpenAiHttp(400, text)) => text.contains("billing"),
        _ => e.downcast_ref::<OpenAiUnreachable>().is_some(),
    }
}

/// OpenAI with its key and, when given, a proxy (`http://` or `socks5://`, with or without user:password@):
/// only these requests go through it, the panel is reached directly.
pub struct OpenAi {
    key: String,
    http: ureq::Agent,
}

impl OpenAi {
    pub fn new(key: &str, proxy: Option<&str>) -> Result<Self> {
        let mut via = None;
        if let Some(p) = proxy.map(str::trim).filter(|p| !p.is_empty()) {
            if !(p.starts_with("http://") || p.starts_with("socks5://")) {
                bail!("proxy must start with http:// or socks5://");
            }
            // never put the address into the error: it may hold a password
            via = Some(parse_proxy(p).ok_or_else(|| anyhow!("bad proxy address"))?);
        }
        Ok(OpenAi { key: key.trim().to_string(), http: crate::net::through(via, TIMEOUT) })
    }

    pub fn edit(&self, model: &str, prompt: &str, images: &[Vec<u8>]) -> Result<Generated> {
        edit(&self.http, &self.key, model, prompt, images)
    }
}

/// The whole request to OpenAI, from the connection to the last byte of the answer.
const TIMEOUT: std::time::Duration = std::time::Duration::from_secs(300);

/// The proxy address, read as ureq 2 read it: `scheme://[user:password@]host[:port]`, the user and the password
/// taken letter for letter (ureq 3 would read `#`, `?` and `%41` in a password as parts of a URL).
/// `socks5://` as it has always worked here: the proxy resolves api.openai.com, this machine does not ask its own
/// DNS for it (ureq 3 calls that socks5h; its plain socks5 resolves locally).
fn parse_proxy(p: &str) -> Option<ureq::Proxy> {
    let p = p.trim_end_matches('/');
    let (protocol, rest) = match p.strip_prefix("http://") {
        Some(rest) => (ureq::ProxyProtocol::Http, rest),
        None => (ureq::ProxyProtocol::Socks5h, p.strip_prefix("socks5://")?),
    };
    let (creds, address) = match rest.rsplit_once('@') {
        Some((creds, address)) => (Some(creds), address),
        None => (None, rest),
    };
    let mut parts = address.split(':');
    let host = parts.next().filter(|h| !h.is_empty())?;
    let mut b = ureq::Proxy::builder(protocol).host(host);
    if let Some(port) = parts.next().and_then(|port| port.parse().ok()) {
        b = b.port(port);                                                  // no port or not a number: 80 / 1080, as before
    }
    if let Some(creds) = creds {
        let (user, password) = creds.split_once(':')?;
        b = b.username(user).password(password);
    }
    b.build().ok()
}

/// The generated frame: a square, as Avito shows previews (the owner 2026-11-09; 1024x1536 before).
pub const IMAGE_SIZE: &str = "1024x1024";

fn edit_fields<'a>(model: &'a str, prompt: &'a str) -> [(&'a str, &'a str); 5] {
    [("model", model), ("prompt", prompt), ("size", IMAGE_SIZE), ("quality", "high"), ("n", "1")]
}

/// Call `/v1/images/edits` with the reference images; returns the generated PNG and usage.
fn edit(http: &ureq::Agent, key: &str, model: &str, prompt: &str, images: &[Vec<u8>]) -> Result<Generated> {
    let files: Vec<(&str, String, &str, &[u8])> = images
        .iter()
        .enumerate()
        .map(|(i, d)| {
            let (mime, ext) = sniff_mime(d);
            ("image[]", format!("ref{i}.{ext}"), mime, d.as_slice())
        })
        .collect();
    let b = boundary();
    let body = multipart(&b, &edit_fields(model, prompt), &files);
    let res = http.post(ENDPOINT)
        .header("Authorization", &format!("Bearer {key}"))
        .header("Content-Type", &format!("multipart/form-data; boundary={b}"))
        .send(&body[..]);
    match res {
        Ok(r) if r.status().is_client_error() || r.status().is_server_error() => {
            let code = r.status().as_u16();
            let text: String = r.into_body().read_to_string().unwrap_or_default().chars().take(400).collect();
            Err(OpenAiHttp(code, text).into())
        }
        Ok(r) => parse_response(&r.into_body().read_to_string().map_err(|e| anyhow!("OpenAI answer not read: {}", crate::net::kind(&e)))?),
        Err(e) => match crate::net::unreached(&e) {
            Some(label) => Err(OpenAiUnreachable(label.to_string()).into()),
            None => bail!("OpenAI request failed: {}", crate::net::kind(&e)),
        },
    }
}

const ENDPOINT: &str = "https://api.openai.com/v1/images/edits";

/// OpenAI was not reached at all (the proxy, the VPN or the network is down).
#[derive(Debug)]
pub struct OpenAiUnreachable(pub String);
impl std::fmt::Display for OpenAiUnreachable {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "OpenAI недоступен ({}): проверьте прокси", self.0)
    }
}
impl std::error::Error for OpenAiUnreachable {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cost_from_usage() {
        let u = Usage { input_tokens: 1700, output_tokens: 1400 };
        assert!((cost_usd(&u, 10.0, 40.0) - 0.073).abs() < 1e-9);
    }

    #[test]
    fn parse_ok_and_missing_image() {
        let g = parse_response(r#"{"data":[{"b64_json":"aGk="}],"usage":{"input_tokens":5,"output_tokens":7}}"#).unwrap();
        assert_eq!(g.png, b"hi");
        assert_eq!((g.usage.input_tokens, g.usage.output_tokens), (5, 7));
        assert!(parse_response(r#"{"data":[]}"#).is_err());
    }

    #[test]
    fn request_asks_for_a_square_frame() {
        assert_eq!(IMAGE_SIZE, "1024x1024");
        let body = String::from_utf8(multipart("B", &edit_fields("m", "p"), &[])).unwrap();
        assert!(body.contains("name=\"size\"\r\n\r\n1024x1024\r\n"), "{body}");
        assert!(!body.contains("1536"));
    }

    #[test]
    fn multipart_has_image_array_parts() {
        let body = multipart("B", &[("model", "m")], &[("image[]", "a.png".into(), "image/png", b"X"), ("image[]", "b.png".into(), "image/png", b"Y")]);
        let s = String::from_utf8(body).unwrap();
        assert_eq!(s.matches("name=\"image[]\"").count(), 2);
        assert!(s.contains("name=\"model\"\r\n\r\nm\r\n") && s.ends_with("--B--\r\n"));
    }

    #[test]
    fn fatal_answers_stop_the_run() {
        let e: anyhow::Error = OpenAiHttp(403, "unsupported_country_region_territory".into()).into();
        assert!(is_fatal(&e.context("job 5")));
        assert!(!is_fatal(&OpenAiHttp(500, String::new()).into()));
        assert!(!is_fatal(&anyhow::anyhow!("panel HTTP 403")));
        assert!(is_fatal(&OpenAiHttp(400, r#"{"error":{"code":"billing_hard_limit_reached"}}"#.into()).into()));
        assert!(!is_fatal(&OpenAiHttp(400, "bad image".into()).into()));
        assert!(is_fatal(&anyhow::Error::from(OpenAiUnreachable("Network Error: Failed to connect".into())).context("job 7")));
        assert_eq!(OpenAiHttp(401, "x".into()).to_string(), "OpenAI HTTP 401: x");
    }

    #[test]
    fn a_dead_proxy_is_unreachable_not_an_attempt() {
        let port = std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();   // closed right away
        let ai = OpenAi::new("k", Some(&format!("socks5://127.0.0.1:{port}"))).unwrap();
        let e = ai.edit("m", "p", &[vec![0x89, b'P', b'N', b'G']]).err().unwrap();
        assert!(is_fatal(&e), "{e:#}");
    }

    /// A fake proxy on 127.0.0.1: reads what the agent sends first, answers with `replies` in turn, hangs up.
    fn fake_proxy(replies: Vec<&'static [u8]>) -> (u16, std::thread::JoinHandle<Vec<Vec<u8>>>) {
        use std::io::Write;
        let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = l.local_addr().unwrap().port();
        let h = std::thread::spawn(move || {
            let (mut s, _) = l.accept().unwrap();
            let mut seen = Vec::new();
            for reply in replies {
                let mut buf = [0u8; 2048];
                let mut got = Vec::new();
                loop {
                    let n = s.read(&mut buf).unwrap();
                    got.extend_from_slice(&buf[..n]);
                    if n == 0 || !got.starts_with(b"CONNECT ") || got.ends_with(b"\r\n\r\n") {   // an HTTP request comes in pieces
                        break;
                    }
                }
                seen.push(got);
                s.write_all(reply).unwrap();
            }
            seen
        });
        (port, h)
    }

    /// `socks5://`: the proxy gets the name api.openai.com and resolves it itself; this machine's DNS is not asked.
    #[test]
    fn socks_proxy_is_given_the_name_of_openai_not_an_address() {
        let (port, h) = fake_proxy(vec![&[5, 0], &[5, 1, 0, 1, 0, 0, 0, 0, 0, 0]]);   // no auth; then "general failure"
        let ai = OpenAi::new("sk-testkey", Some(&format!("socks5://127.0.0.1:{port}"))).unwrap();
        let e = ai.edit("m", "p", &[vec![0x89, b'P', b'N', b'G']]).err().unwrap();
        let seen = h.join().unwrap();
        let mut want = vec![5, 1, 0, 3, 14];                                          // CONNECT, by domain name, 14 bytes
        want.extend(b"api.openai.com");
        want.extend(443u16.to_be_bytes());
        assert_eq!(seen[1], want);
        assert!(is_fatal(&e), "{e:#}");                                               // nothing was sent to OpenAI
        assert!(!format!("{e:#}").contains("testkey") && !format!("{e:#}").contains(&port.to_string()), "{e:#}");
    }

    /// `http://` with a password: CONNECT to api.openai.com:443; the key is not shown to the proxy, a refusal is "unreachable".
    #[test]
    fn http_proxy_gets_a_connect_to_openai_and_never_the_key() {
        let (port, h) = fake_proxy(vec![b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n"]);
        let ai = OpenAi::new("sk-testkey", Some(&format!("http://user:SECRETPW@127.0.0.1:{port}"))).unwrap();
        let e = ai.edit("m", "p", &[vec![0x89, b'P', b'N', b'G']]).err().unwrap();
        let asked = String::from_utf8(h.join().unwrap().remove(0)).unwrap();
        assert!(asked.starts_with("CONNECT api.openai.com:443 HTTP/1.1\r\n"), "{asked}");
        assert!(asked.contains("Proxy-Authorization: Basic ") && !asked.contains("testkey"), "{asked}");
        assert!(is_fatal(&e), "{e:#}");
        let text = format!("{e:#}");
        assert!(text.contains("Provided proxy credentials are incorrect") && !text.contains("SECRETPW") && !text.contains("testkey"), "{text}");
    }

    #[test]
    fn the_whole_request_has_five_minutes_and_the_address_is_fixed() {
        assert_eq!(TIMEOUT, std::time::Duration::from_secs(300));
        assert_eq!(ENDPOINT, "https://api.openai.com/v1/images/edits");
    }

    /// The address is read as before: remote DNS for socks5, default ports, the password letter for letter.
    #[test]
    fn proxy_address_is_read_as_before() {
        let p = parse_proxy("socks5://u:p@proxy.example:1081/").unwrap();
        assert_eq!((p.protocol(), p.host(), p.port(), p.resolve_target()), (ureq::ProxyProtocol::Socks5h, "proxy.example", 1081, false));
        assert_eq!((p.username(), p.password()), (Some("u"), Some("p")));
        let p = parse_proxy("socks5://10.0.0.1").unwrap();
        assert_eq!((p.host(), p.port(), p.username()), ("10.0.0.1", 1080, None));
        assert_eq!(parse_proxy("http://proxy.example").unwrap().port(), 80);
        for password in ["pa#ss", "a?b", "ab%41", "x^{}|<>`\"y", "p:q", "пароль"] {
            let p = parse_proxy(&format!("http://user:{password}@192.0.2.7:3128")).unwrap();
            assert_eq!((p.host(), p.port(), p.username(), p.password()), ("192.0.2.7", 3128, Some("user"), Some(password)), "{password}");
        }
        assert!(parse_proxy("http://useronly@h:1").is_none() && parse_proxy("http://").is_none() && parse_proxy("ftp://h:1").is_none());
    }

    /// The password reaches the proxy exactly as typed in the panel.
    #[test]
    fn http_proxy_gets_the_password_letter_for_letter() {
        let (port, h) = fake_proxy(vec![b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n"]);
        let ai = OpenAi::new("k", Some(&format!("http://user:pa#ss%41@127.0.0.1:{port}"))).unwrap();
        assert!(ai.edit("m", "p", &[vec![0x89, b'P', b'N', b'G']]).is_err());
        let asked = String::from_utf8(h.join().unwrap().remove(0)).unwrap();
        let want = base64::engine::general_purpose::STANDARD.encode("user:pa#ss%41");
        assert!(asked.contains(&format!("Proxy-Authorization: Basic {want}\r\n")), "{asked}");
    }

    #[test]
    fn proxy_schemes_and_no_address_in_errors() {
        assert!(OpenAi::new("k", None).is_ok() && OpenAi::new("k", Some(" ")).is_ok());
        assert!(OpenAi::new("k", Some("http://u:p@127.0.0.1:3128")).is_ok());
        assert!(OpenAi::new("k", Some("socks5://127.0.0.1:1080")).is_ok());
        let e = OpenAi::new("k", Some("ftp://user:SECRETPW@h:1")).err().unwrap().to_string();
        assert!(!e.contains("SECRETPW"), "{e}");
    }

    #[test]
    fn sniff() {
        assert_eq!(sniff_mime(&[0x89, b'P', b'N', b'G', 0]).0, "image/png");
        assert_eq!(sniff_mime(&[0xff, 0xd8]).0, "image/jpeg");
    }
}
