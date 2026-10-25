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
        let mut b = ureq::AgentBuilder::new();
        if let Some(p) = proxy.map(str::trim).filter(|p| !p.is_empty()) {
            if !(p.starts_with("http://") || p.starts_with("socks5://")) {
                bail!("proxy must start with http:// or socks5://");
            }
            // never put the address into the error: it may hold a password
            b = b.proxy(ureq::Proxy::new(p).map_err(|_| anyhow!("bad proxy address"))?);
        }
        Ok(OpenAi { key: key.trim().to_string(), http: b.build() })
    }

    pub fn edit(&self, model: &str, prompt: &str, images: &[Vec<u8>]) -> Result<Generated> {
        edit(&self.http, &self.key, model, prompt, images)
    }
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
    let body = multipart(
        &b,
        &[("model", model), ("prompt", prompt), ("size", "1024x1536"), ("quality", "high"), ("n", "1")],
        &files,
    );
    let res = http.post("https://api.openai.com/v1/images/edits")
        .set("Authorization", &format!("Bearer {key}"))
        .set("Content-Type", &format!("multipart/form-data; boundary={b}"))
        .timeout(std::time::Duration::from_secs(300))
        .send_bytes(&body);
    match res {
        Ok(r) => parse_response(&r.into_string()?),
        Err(ureq::Error::Status(code, r)) => {
            let text: String = r.into_string().unwrap_or_default().chars().take(400).collect();
            Err(OpenAiHttp(code, text).into())
        }
        Err(e) if UNREACHABLE.contains(&e.kind()) => Err(OpenAiUnreachable(e.kind().to_string()).into()),
        Err(e) => bail!("OpenAI request failed: {}", e.kind()),
    }
}

/// Failures before anything reached OpenAI (the proxy or the network is down): nothing is paid for.
const UNREACHABLE: [ureq::ErrorKind; 5] = [
    ureq::ErrorKind::Dns,
    ureq::ErrorKind::ConnectionFailed,
    ureq::ErrorKind::ProxyConnect,
    ureq::ErrorKind::ProxyUnauthorized,
    ureq::ErrorKind::InvalidProxyUrl,
];

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
