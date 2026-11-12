use crate::openai::{boundary, multipart};
use crate::prompt::Job;
use anyhow::Result;
use std::io::Read;

const MAX_DOWNLOAD: u64 = 30_000_000;

/// Non-2xx answer from the panel.
#[derive(Debug)]
pub struct PanelStatus(pub u16);
impl std::fmt::Display for PanelStatus {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "panel HTTP {}", self.0)
    }
}
impl std::error::Error for PanelStatus {}

/// True when the error is a panel answer that retrying cannot fix (4xx).
pub fn is_client_error(e: &anyhow::Error) -> bool {
    matches!(e.downcast_ref::<PanelStatus>(), Some(PanelStatus(c)) if (400..500).contains(c))
}

pub fn is_conflict(e: &anyhow::Error) -> bool {
    matches!(e.downcast_ref::<PanelStatus>(), Some(PanelStatus(409)))
}

/// (scheme, host, port) of an http(s) URL; None if it is not one.
fn origin(url: &str) -> Option<(String, String, u16)> {
    let (scheme, rest) = url.split_once("://")?;
    let scheme = scheme.to_ascii_lowercase();
    let default = match scheme.as_str() {
        "http" => 80,
        "https" => 443,
        _ => return None,
    };
    let auth = rest.split(['/', '?', '#']).next()?;
    let auth = auth.rsplit('@').next()?;
    let (host, port) = match auth.rsplit_once(':') {
        Some((h, p)) if !p.is_empty() && p.chars().all(|c| c.is_ascii_digit()) => (h, p.parse().ok()?),
        _ => (auth, default),
    };
    if host.is_empty() {
        return None;
    }
    Some((scheme, host.to_ascii_lowercase(), port))
}

/// Same scheme, host and port (no string-prefix matching).
pub fn same_origin(a: &str, b: &str) -> bool {
    matches!((origin(a), origin(b)), (Some(x), Some(y)) if x == y)
}

/// `/api/image-jobs/work`: scope "all" = the whole queue, "rush" = only the models asked for by name.
#[derive(Debug, Clone, Default, serde::Deserialize, PartialEq)]
pub struct Work {
    pub run: bool,
    #[serde(default)]
    pub scope: String,
    #[serde(default)]
    pub request_at: String,
    #[serde(default)]
    pub copies: bool,
    #[serde(default)]
    pub copies_at: String,
}

pub fn jobs_url(base: &str, limit: u32, only_rush: bool) -> String {
    format!("{base}/api/image-jobs?limit={limit}{}", if only_rush { "&only=rush" } else { "" })
}

/// Client for the panel agent API. The token is only ever sent to the panel host.
pub struct Panel {
    base: String,
    token: String,
    http: ureq::Agent,
}

impl Panel {
    pub fn new(base: &str, token: &str) -> Self {
        Panel { base: base.trim_end_matches('/').to_string(), token: token.trim().to_string(), http: crate::net::direct() }
    }

    fn auth(&self) -> String {
        format!("Bearer {}", self.token)
    }

    pub fn jobs(&self, limit: u32, only_rush: bool) -> Result<Vec<Job>> {
        let r = self.http.get(&jobs_url(&self.base, limit, only_rush))
            .header("Authorization", &self.auth())
            .call()
            .map_err(describe)?;
        Ok(serde_json::from_str(&r.into_body().read_to_string()?)?)
    }

    /// The run the owner asked for in the panel, if any.
    pub fn work(&self) -> Result<Work> {
        let r = self.http.get(&format!("{}/api/image-jobs/work", self.base)).header("Authorization", &self.auth()).call().map_err(describe)?;
        Ok(serde_json::from_str(&r.into_body().read_to_string()?)?)
    }

    /// End of a run: the panel clears the request it handed out and shows the result.
    pub fn run_done(&self, w: &Work, done: u32, failed: u32, message: &str) -> Result<()> {
        let msg: String = redact(message).chars().take(300).collect();
        let (done, failed) = (done.to_string(), failed.to_string());
        self.http.post(&format!("{}/api/image-jobs/run-done", self.base))
            .header("Authorization", &self.auth())
            .send_form([("request_at", &w.request_at), ("copies_at", &w.copies_at), ("done", &done), ("failed", &failed), ("message", &msg)])
            .map_err(describe)?;
        Ok(())
    }

    /// Download an image; the Bearer header goes only to panel URLs.
    pub fn download(&self, url: &str) -> Result<Vec<u8>> {
        let mut req = crate::net::agent(None, None, 0, true).get(url);
        if same_origin(url, &self.base) {
            req = req.header("Authorization", &self.auth());
        }
        let r = req.call().map_err(describe)?;
        let mut buf = Vec::new();
        r.into_body().into_reader().take(MAX_DOWNLOAD + 1).read_to_end(&mut buf)?;
        if buf.len() as u64 > MAX_DOWNLOAD {
            anyhow::bail!("download over {} MB", MAX_DOWNLOAD / 1_000_000);
        }
        Ok(buf)
    }

    pub fn result(&self, job_id: i64, png: &[u8], cost: f64) -> Result<()> {
        let b = boundary();
        let cost_s = format!("{cost:.4}");
        let body = multipart(&b, &[("cost_usd", &cost_s)], &[("file", "result.png".into(), "image/png", png)]);
        self.http.post(&format!("{}/api/image-jobs/{job_id}/result", self.base))
            .header("Authorization", &self.auth())
            .header("Content-Type", &format!("multipart/form-data; boundary={b}"))
            .send(&body[..])
            .map_err(describe)?;
        Ok(())
    }

    pub fn copies_todo(&self) -> Result<Vec<crate::copies::Todo>> {
        let r = self.http.get(&format!("{}/api/photo-copies/todo", self.base)).header("Authorization", &self.auth()).call().map_err(describe)?;
        Ok(serde_json::from_str(&r.into_body().read_to_string()?)?)
    }

    pub fn upload_copy(&self, t: &crate::copies::Todo, jpeg: &[u8]) -> Result<()> {
        let b = boundary();
        let (acct, src) = (t.account_id.to_string(), t.source_photo_id.to_string());
        let fields = [("account_id", acct.as_str()), ("source_photo_id", src.as_str()), ("kind", t.kind.as_str()), ("variant", t.variant.as_str())];
        let body = multipart(&b, &fields, &[("file", "copy.jpg".into(), "image/jpeg", jpeg)]);
        self.http.post(&format!("{}/api/photo-copies", self.base))
            .header("Authorization", &self.auth())
            .header("Content-Type", &format!("multipart/form-data; boundary={b}"))
            .send(&body[..])
            .map_err(describe)?;
        Ok(())
    }

    pub fn review_sheet(&self, set_key: &str, jpeg: &[u8]) -> Result<()> {
        let b = boundary();
        let body = multipart(&b, &[("set_key", set_key)], &[("file", "sheet.jpg".into(), "image/jpeg", jpeg)]);
        self.http.post(&format!("{}/api/review-sheet", self.base))
            .header("Authorization", &self.auth())
            .header("Content-Type", &format!("multipart/form-data; boundary={b}"))
            .send(&body[..])
            .map_err(describe)?;
        Ok(())
    }

    pub fn is_panel_url(&self, url: &str) -> bool {
        same_origin(url, &self.base)
    }

    /// `requeue`: nothing was paid and the job is not to blame (OpenAI unreachable, key or money): back to the queue, not an attempt.
    pub fn fail(&self, job_id: i64, error: &str, cost: Option<f64>, requeue: bool) -> Result<()> {
        let msg: String = redact(error).chars().take(400).collect();
        let cost_s = format!("{:.4}", cost.unwrap_or(0.0));
        self.http.post(&format!("{}/api/image-jobs/{job_id}/fail", self.base))
            .header("Authorization", &self.auth())
            .send_form([("error", msg.as_str()), ("cost_usd", cost_s.as_str()), ("requeue", if requeue { "1" } else { "0" })])
            .map_err(describe)?;
        Ok(())
    }
}

fn describe(e: ureq::Error) -> anyhow::Error {
    match e {
        ureq::Error::StatusCode(code) => anyhow::Error::new(PanelStatus(code)),
        other => anyhow::anyhow!("panel request failed: {}", crate::net::kind(&other)),
    }
}

/// Replace anything that looks like an OpenAI key (`sk-...`) with `sk-***`.
pub fn redact(text: &str) -> String {
    let mut out = String::new();
    let mut rest = text;
    while let Some(i) = rest.find("sk-") {
        out.push_str(&rest[..i]);
        let tail = &rest[i + 3..];
        if rest[..i].chars().next_back().is_some_and(|c| c.is_ascii_alphanumeric()) {
            out.push_str("sk-");     // part of a longer word (e.g. "task-1")
            rest = tail;
            continue;
        }
        let n = tail.find(|c: char| !(c.is_ascii_alphanumeric() || "_*.-".contains(c))).unwrap_or(tail.len());
        if n == 0 {
            out.push_str("sk-");
        } else {
            out.push_str("sk-***");
        }
        rest = &tail[n..];
    }
    out.push_str(rest);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn origin_compare() {
        let p = "https://panel.example";
        assert!(same_origin("https://panel.example/api/x?a=1", p));
        assert!(same_origin("HTTPS://Panel.Example:443/x", p));
        assert!(!same_origin("https://panel.example.evil.com/x", p));
        assert!(!same_origin("http://panel.example/x", p));
        assert!(!same_origin("https://panel.example:8443/x", p));
        assert!(!same_origin("https://panel.example@evil.com/x", p));
        assert!(!same_origin("ftp://panel.example/x", p));
        assert!(same_origin("http://127.0.0.1:5000/a", "http://127.0.0.1:5000"));
    }

    /// A local server: answers every connection with `answer`, returns the requests it saw.
    fn serve(answer: &'static str, hits: usize) -> (String, std::thread::JoinHandle<Vec<String>>) {
        use std::io::Write;
        let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", l.local_addr().unwrap());
        let h = std::thread::spawn(move || {
            let mut seen = Vec::new();
            for _ in 0..hits {
                let (mut s, _) = l.accept().unwrap();
                let mut buf = [0u8; 4096];
                let n = s.read(&mut buf).unwrap();
                seen.push(String::from_utf8_lossy(&buf[..n]).to_lowercase());
                s.write_all(answer.as_bytes()).unwrap();
            }
            seen
        });
        (url, h)
    }

    const OK: &str = "HTTP/1.1 200 OK\r\nContent-Length: 3\r\nConnection: close\r\n\r\nimg";

    #[test]
    fn the_token_goes_to_the_panel_and_nowhere_else() {
        let (panel_url, panel_seen) = serve(OK, 2);
        let (other_url, other_seen) = serve(OK, 1);
        let panel = Panel::new(&panel_url, "TOKEN123");
        assert_eq!(panel.download(&format!("{panel_url}/photo/1.jpg")).unwrap(), b"img");
        assert_eq!(panel.download(&format!("{other_url}/photo/1.jpg")).unwrap(), b"img");
        assert!(panel.work().is_err());                                    // "img" is not JSON; the request itself is what matters
        let panel_seen = panel_seen.join().unwrap();
        assert!(panel_seen.iter().all(|r| r.contains("authorization: bearer token123")), "{panel_seen:?}");
        let other = other_seen.join().unwrap().remove(0);
        assert!(!other.contains("authorization") && !other.contains("token123"), "{other}");
    }

    /// A redirect from the panel is not followed: the token and the request never go where it points.
    #[test]
    fn a_download_does_not_follow_redirects() {
        let (other_url, _other) = serve(OK, 1);
        let answer: &'static str = Box::leak(format!("HTTP/1.1 302 Found\r\nLocation: {other_url}/x\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").into_boxed_str());
        let (panel_url, panel_seen) = serve(answer, 1);
        let got = Panel::new(&panel_url, "TOKEN123").download(&format!("{panel_url}/photo/1.jpg")).unwrap();
        assert!(got.is_empty());
        assert_eq!(panel_seen.join().unwrap().len(), 1);
        let l = std::net::TcpStream::connect(other_url.trim_start_matches("http://")).unwrap();   // the first visitor of the other server is this test
        drop(l);
    }

    #[test]
    fn a_panel_refusal_is_its_status_and_a_dead_panel_names_no_address() {
        let (url, h) = serve("HTTP/1.1 409 Conflict\r\nContent-Length: 0\r\nConnection: close\r\n\r\n", 1);
        let e = Panel::new(&url, "TOKEN123").result(5, b"png", 0.1).err().unwrap();
        assert!(is_conflict(&e) && is_client_error(&e), "{e:#}");
        h.join().unwrap();
        let port = std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
        let e = Panel::new(&format!("http://127.0.0.1:{port}"), "TOKEN123").work().err().unwrap();
        assert_eq!(format!("{e:#}"), "panel request failed: Connection Failed");
    }

    #[test]
    fn work_parses_and_jobs_url() {
        let w: Work = serde_json::from_str(r#"{"run":true,"scope":"rush","request_at":"2026-10-07T11:00:00","copies":false,"copies_at":""}"#).unwrap();
        assert!(w.run && w.scope == "rush" && !w.copies);
        assert_eq!(jobs_url("https://p", 1, true), "https://p/api/image-jobs?limit=1&only=rush");
        assert_eq!(jobs_url("https://p", 3, false), "https://p/api/image-jobs?limit=3");
    }

    #[test]
    fn redacts_keys() {
        let r = redact("bad key sk-proj-AbC_12.3-x*y here, sk- alone, task-1");
        assert_eq!(r, "bad key sk-*** here, sk- alone, task-1");
        assert!(!r.contains("AbC"));
    }
}
