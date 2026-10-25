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
}

impl Panel {
    pub fn new(base: &str, token: &str) -> Self {
        Panel { base: base.trim_end_matches('/').to_string(), token: token.trim().to_string() }
    }

    fn auth(&self) -> String {
        format!("Bearer {}", self.token)
    }

    pub fn jobs(&self, limit: u32, only_rush: bool) -> Result<Vec<Job>> {
        let r = ureq::get(&jobs_url(&self.base, limit, only_rush))
            .set("Authorization", &self.auth())
            .call()
            .map_err(describe)?;
        Ok(serde_json::from_str(&r.into_string()?)?)
    }

    /// The run the owner asked for in the panel, if any.
    pub fn work(&self) -> Result<Work> {
        let r = ureq::get(&format!("{}/api/image-jobs/work", self.base)).set("Authorization", &self.auth()).call().map_err(describe)?;
        Ok(serde_json::from_str(&r.into_string()?)?)
    }

    /// End of a run: the panel clears the request it handed out and shows the result.
    pub fn run_done(&self, w: &Work, done: u32, failed: u32, message: &str) -> Result<()> {
        let msg: String = redact(message).chars().take(300).collect();
        let (done, failed) = (done.to_string(), failed.to_string());
        ureq::post(&format!("{}/api/image-jobs/run-done", self.base))
            .set("Authorization", &self.auth())
            .send_form(&[("request_at", &w.request_at), ("copies_at", &w.copies_at), ("done", &done), ("failed", &failed), ("message", &msg)])
            .map_err(describe)?;
        Ok(())
    }

    /// Download an image; the Bearer header goes only to panel URLs.
    pub fn download(&self, url: &str) -> Result<Vec<u8>> {
        let mut req = ureq::AgentBuilder::new().redirects(0).build().get(url);
        if same_origin(url, &self.base) {
            req = req.set("Authorization", &self.auth());
        }
        let r = req.call().map_err(describe)?;
        let mut buf = Vec::new();
        r.into_reader().take(MAX_DOWNLOAD + 1).read_to_end(&mut buf)?;
        if buf.len() as u64 > MAX_DOWNLOAD {
            anyhow::bail!("download over {} MB", MAX_DOWNLOAD / 1_000_000);
        }
        Ok(buf)
    }

    pub fn result(&self, job_id: i64, png: &[u8], cost: f64) -> Result<()> {
        let b = boundary();
        let cost_s = format!("{cost:.4}");
        let body = multipart(&b, &[("cost_usd", &cost_s)], &[("file", "result.png".into(), "image/png", png)]);
        ureq::post(&format!("{}/api/image-jobs/{job_id}/result", self.base))
            .set("Authorization", &self.auth())
            .set("Content-Type", &format!("multipart/form-data; boundary={b}"))
            .send_bytes(&body)
            .map_err(describe)?;
        Ok(())
    }

    pub fn copies_todo(&self) -> Result<Vec<crate::copies::Todo>> {
        let r = ureq::get(&format!("{}/api/photo-copies/todo", self.base)).set("Authorization", &self.auth()).call().map_err(describe)?;
        Ok(serde_json::from_str(&r.into_string()?)?)
    }

    pub fn upload_copy(&self, t: &crate::copies::Todo, jpeg: &[u8]) -> Result<()> {
        let b = boundary();
        let (acct, src) = (t.account_id.to_string(), t.source_photo_id.to_string());
        let fields = [("account_id", acct.as_str()), ("source_photo_id", src.as_str()), ("kind", t.kind.as_str()), ("variant", t.variant.as_str())];
        let body = multipart(&b, &fields, &[("file", "copy.jpg".into(), "image/jpeg", jpeg)]);
        ureq::post(&format!("{}/api/photo-copies", self.base))
            .set("Authorization", &self.auth())
            .set("Content-Type", &format!("multipart/form-data; boundary={b}"))
            .send_bytes(&body)
            .map_err(describe)?;
        Ok(())
    }

    pub fn review_sheet(&self, set_key: &str, jpeg: &[u8]) -> Result<()> {
        let b = boundary();
        let body = multipart(&b, &[("set_key", set_key)], &[("file", "sheet.jpg".into(), "image/jpeg", jpeg)]);
        ureq::post(&format!("{}/api/review-sheet", self.base))
            .set("Authorization", &self.auth())
            .set("Content-Type", &format!("multipart/form-data; boundary={b}"))
            .send_bytes(&body)
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
        ureq::post(&format!("{}/api/image-jobs/{job_id}/fail", self.base))
            .set("Authorization", &self.auth())
            .send_form(&[("error", &msg), ("cost_usd", &cost_s), ("requeue", if requeue { "1" } else { "0" })])
            .map_err(describe)?;
        Ok(())
    }
}

fn describe(e: ureq::Error) -> anyhow::Error {
    match e {
        ureq::Error::Status(code, _) => anyhow::Error::new(PanelStatus(code)),
        other => anyhow::anyhow!("panel request failed: {}", other.kind()),
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
