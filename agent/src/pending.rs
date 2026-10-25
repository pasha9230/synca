//! Paid-but-not-uploaded results, kept on disk so the money is never lost.
use anyhow::Result;
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::time::Duration;

#[derive(Debug, Serialize, Deserialize, PartialEq)]
pub struct Meta {
    pub job_id: i64,
    pub cost_usd: f64,
}

pub struct Pending {
    pub meta: Meta,
    pub png: PathBuf,
    pub json: PathBuf,
}

pub fn save(dir: &Path, job_id: i64, png: &[u8], cost_usd: f64) -> Result<()> {
    use std::os::unix::fs::DirBuilderExt;
    std::fs::DirBuilder::new().recursive(true).mode(0o700).create(dir)?;
    std::fs::write(dir.join(format!("{job_id}.png")), png)?;
    // json last: a listed entry always has its image
    std::fs::write(dir.join(format!("{job_id}.json")), serde_json::to_vec(&Meta { job_id, cost_usd })?)?;
    Ok(())
}

pub fn list(dir: &Path) -> Vec<Pending> {
    let mut out = Vec::new();
    let Ok(rd) = std::fs::read_dir(dir) else { return out };
    for e in rd.flatten() {
        let json = e.path();
        if json.extension().and_then(|x| x.to_str()) != Some("json") {
            continue;
        }
        let png = json.with_extension("png");
        let meta = std::fs::read(&json).ok().and_then(|b| serde_json::from_slice::<Meta>(&b).ok());
        if let (Some(meta), true) = (meta, png.exists()) {
            out.push(Pending { meta, png, json });
        }
    }
    out.sort_by_key(|p| p.meta.job_id);
    out
}

pub fn remove(p: &Pending) {
    let _ = std::fs::remove_file(&p.png);
    let _ = std::fs::remove_file(&p.json);
}

/// Run `f` up to `delays.len() + 1` times, sleeping between attempts; `give_up` stops early.
pub fn with_retries<T>(
    delays: &[Duration],
    give_up: impl Fn(&anyhow::Error) -> bool,
    mut f: impl FnMut() -> Result<T>,
) -> Result<T> {
    let mut i = 0;
    loop {
        match f() {
            Ok(v) => return Ok(v),
            Err(e) if i >= delays.len() || give_up(&e) => return Err(e),
            Err(_) => {
                std::thread::sleep(delays[i]);
                i += 1;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("tpa-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        d
    }

    #[test]
    fn save_list_remove_roundtrip() {
        use std::os::unix::fs::PermissionsExt;
        let d = tmp("pend");
        save(&d, 7, b"png", 0.073).unwrap();
        assert_eq!(std::fs::metadata(&d).unwrap().permissions().mode() & 0o777, 0o700);
        let l = list(&d);
        assert_eq!(l.len(), 1);
        assert_eq!(l[0].meta, Meta { job_id: 7, cost_usd: 0.073 });
        assert_eq!(std::fs::read(&l[0].png).unwrap(), b"png");
        remove(&l[0]);
        assert!(list(&d).is_empty() && !d.join("7.png").exists());
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn list_missing_dir_is_empty() {
        assert!(list(&tmp("none")).is_empty());
    }

    #[test]
    fn retries_then_succeeds_and_gives_up() {
        let z = [Duration::ZERO; 3];
        let mut n = 0;
        let r = with_retries(&z, |_| false, || { n += 1; if n < 3 { anyhow::bail!("x") } else { Ok(n) } });
        assert_eq!(r.unwrap(), 3);
        let mut n = 0;
        let r: Result<()> = with_retries(&z, |_| false, || { n += 1; anyhow::bail!("x") });
        assert!(r.is_err() && n == 4);
        let mut n = 0;
        let r: Result<()> = with_retries(&z, |_| true, || { n += 1; anyhow::bail!("x") });
        assert!(r.is_err() && n == 1);
    }
}
