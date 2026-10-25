mod api;
mod card;
mod copies;
mod openai;
mod pending;
mod prompt;
mod sheet;
mod variants;

use anyhow::{anyhow, bail, Context, Result};
use std::path::{Path, PathBuf};
use std::time::Duration;

fn home_file(rel: &str) -> Result<String> {
    let home = std::env::var("HOME").context("HOME is not set")?;
    let path = PathBuf::from(home).join(rel);
    let text = std::fs::read_to_string(&path).with_context(|| format!("cannot read {}", path.display()))?;
    Ok(text.trim().to_string())
}

fn env_f64(name: &str, default: f64) -> f64 {
    std::env::var(name).ok().and_then(|v| v.trim().parse().ok()).unwrap_or(default)
}

fn panel_from_env() -> Result<api::Panel> {
    let panel_url = std::env::var("TIRES_PANEL_URL").map_err(|_| anyhow!("TIRES_PANEL_URL is not set"))?;
    Ok(api::Panel::new(&panel_url, &home_file(".config/tires/agent-token")?))
}

fn generate(limit: u32) -> Result<()> {
    let panel = panel_from_env()?;
    let ai = openai::OpenAi::new(&home_file(".config/openai/key")?, None)?;
    let pdir = pending_dir()?;
    retry_pending(&panel, &pdir);
    let jobs = panel.jobs(limit, false)?;
    println!("jobs: {}", jobs.len());
    for job in jobs {
        let _ = do_job(&panel, &pdir, &ai, &job);         // a failure is reported to the panel and logged inside
    }
    Ok(())
}

/// One job end to end: the result or the failure (with the money spent) goes to the panel. Err = the failure.
fn do_job(panel: &api::Panel, pdir: &Path, ai: &openai::OpenAi, job: &prompt::Job) -> std::result::Result<(), anyhow::Error> {
    let model = std::env::var("TIRES_IMAGE_MODEL").unwrap_or_else(|_| "gpt-image-2.5-flare".into());
    match run_job(panel, pdir, ai, &model, job, env_f64("TIRES_PRICE_IN", 10.0), env_f64("TIRES_PRICE_OUT", 40.0)) {
        Ok(cost) => {
            println!("job {} ({}) done, ${:.4}", job.id, job.kind, cost);
            Ok(())
        }
        Err(e) => {
            let msg = api::redact(&format!("{e:#}"));
            eprintln!("job {} failed: {msg}", job.id);
            let requeue = openai::is_fatal(&e) && spent_of(&e).is_none();
            if let Err(e2) = panel.fail(job.id, &msg, spent_of(&e), requeue) {
                eprintln!("job {}: could not report failure: {e2:#}", job.id);
            }
            Err(e)
        }
    }
}

/// Most jobs one run takes (the panel's daily cap stops it long before).
const MAX_RUN_JOBS: u32 = 100;

/// Jobs one by one until the panel has none left (`only_rush`: only the models asked for by name).
/// (done, failed, why it stopped early); a bad key, a closed region or no quota stops at the first job.
fn run_queue(panel: &api::Panel, pdir: &Path, ai: &openai::OpenAi, only_rush: bool) -> (u32, u32, String) {
    let (mut done, mut failed) = (0, 0);
    while done + failed < MAX_RUN_JOBS {
        let jobs = match panel.jobs(1, only_rush) {
            Ok(j) => j,
            Err(e) => return (done, failed, format!("панель не выдала задания: {}", api::redact(&format!("{e:#}")))),
        };
        let Some(job) = jobs.first() else { break };
        match do_job(panel, pdir, ai, job) {
            Ok(()) => done += 1,
            Err(e) => {
                failed += 1;
                if openai::is_fatal(&e) {
                    return (done, failed, format!("генерация остановлена: {}", api::redact(&format!("{e:#}"))));
                }
            }
        }
    }
    (done, failed, String::new())
}

/// `tick`: ask the panel whether the owner pressed «Сгенерировать»; nothing asked = exit at once.
/// Asked: the jobs, then the free copies and cards, then the result goes back to the panel.
/// What the server's wrapper (`python -m tires_sync photo-agent-tick`) writes to stdin: nothing of it is on disk, in env or in argv.
#[derive(serde::Deserialize)]
struct Secrets {
    token: String,
    #[serde(default)]
    openai_key: String,
    #[serde(default)]
    proxy: String,
}

fn read_secrets(input: impl std::io::Read) -> Result<Secrets> {
    use std::io::Read;
    let mut text = String::new();
    input.take(64 * 1024).read_to_string(&mut text)?;
    serde_json::from_str(&text).map_err(|_| anyhow!("stdin: bad secrets JSON"))
}

fn tick(args: &[String]) -> Result<()> {
    if !args.iter().any(|a| a == "--stdin-secrets") {
        let ai = home_file(".config/openai/key").ok().map(|k| openai::OpenAi::new(&k, None)).transpose()?;
        return tick_with(&panel_from_env()?, ai, &pending_dir()?);
    }
    let s = read_secrets(std::io::stdin())?;
    let panel_url = std::env::var("TIRES_PANEL_URL").map_err(|_| anyhow!("TIRES_PANEL_URL is not set"))?;
    let panel = api::Panel::new(&panel_url, &s.token);
    let ai = match openai::OpenAi::new(&s.openai_key, Some(&s.proxy)) {
        Ok(ai) if !s.openai_key.trim().is_empty() => Some(ai),
        Ok(_) => None,
        Err(e) => {
            // a bad proxy: say so in the panel instead of a silent failure
            let work = panel.work()?;
            return panel.run_done(&work, 0, 0, &format!("прокси для OpenAI: {e}"));
        }
    };
    tick_with(&panel, ai, &pending_dir()?)
}

fn tick_with(panel: &api::Panel, ai: Option<openai::OpenAi>, pdir: &Path) -> Result<()> {
    let work = panel.work()?;
    if !work.run && !work.copies {
        return Ok(());
    }
    let (mut done, mut failed, mut message) = (0, 0, String::new());
    if work.run {
        match ai {
            Some(ai) => {
                retry_pending(panel, pdir);
                (done, failed, message) = run_queue(panel, pdir, &ai, work.scope == "rush");
            }
            None => message = "нет ключа OpenAI: «Подключения → OpenAI (фото)»".into(),
        }
    }
    if let Err(e) = copies::run_with(panel) {
        let e = api::redact(&format!("копии и карточки: {e:#}"));
        eprintln!("{e}");
        if message.is_empty() {
            message = e;
        }
    }
    println!("run: done {done}, failed {failed}{}", if message.is_empty() { String::new() } else { format!(", {message}") });
    panel.run_done(&work, done, failed, &message)
}

/// Error that carries the cost already paid to OpenAI.
#[derive(Debug)]
struct Paid(f64, anyhow::Error);
impl std::fmt::Display for Paid {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{:#}", self.1)
    }
}
impl std::error::Error for Paid {}

fn spent_of(e: &anyhow::Error) -> Option<f64> {
    e.downcast_ref::<Paid>().map(|p| p.0)
}

/// The agent's own folder: `TIRES_AGENT_DIR` (the server: /var/lib/tires-sync/agent), else ~/.config/tires (the Mac).
fn agent_dir() -> Result<PathBuf> {
    match std::env::var("TIRES_AGENT_DIR") {
        Ok(d) if !d.trim().is_empty() => Ok(PathBuf::from(d)),
        _ => Ok(PathBuf::from(std::env::var("HOME").context("HOME is not set")?).join(".config/tires")),
    }
}

fn pending_dir() -> Result<PathBuf> {
    Ok(agent_dir()?.join("pending"))
}

/// Photos of the pickup point used as the scene: <agent dir>/backgrounds/*.jpg|png.
fn backgrounds_dir() -> Result<PathBuf> {
    Ok(agent_dir()?.join("backgrounds"))
}

/// One background per job, alternating by job id; none when the folder is empty or missing.
fn pick_background(dir: &Path, job_id: i64) -> Option<PathBuf> {
    let mut files: Vec<PathBuf> = std::fs::read_dir(dir)
        .ok()?
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| matches!(p.extension().and_then(|e| e.to_str()), Some("jpg" | "jpeg" | "png")))
        .collect();
    files.sort();
    let n = files.len() as i64;
    (n > 0).then(|| files[job_id.rem_euclid(n) as usize].clone())
}

const RETRY_DELAYS:[Duration; 3] = [Duration::from_secs(1), Duration::from_secs(4), Duration::from_secs(10)];

/// Upload results that were paid for but never reached the panel.
fn retry_pending(panel: &api::Panel, dir: &Path) {
    for p in pending::list(dir) {
        match panel.result(p.meta.job_id, &std::fs::read(&p.png).unwrap_or_default(), p.meta.cost_usd) {
            Ok(()) => {
                pending::remove(&p);
                println!("pending job {} uploaded", p.meta.job_id);
            }
            Err(e) if api::is_conflict(&e) => println!("pending job {} rejected (409), kept: {}", p.meta.job_id, p.png.display()),
            Err(e) => eprintln!("pending job {}: {}", p.meta.job_id, api::redact(&format!("{e:#}"))),
        }
    }
}

fn run_job(panel: &api::Panel, pdir: &Path, ai: &openai::OpenAi, model: &str, job: &prompt::Job, pin: f64, pout: f64) -> Result<f64> {
    if !panel.is_panel_url(&job.ref_url) {
        bail!("ref_url host differs from TIRES_PANEL_URL: public_base and TIRES_PANEL_URL differ");
    }
    let mut images = vec![panel.download(&job.ref_url).context("reference download")?];
    if job.kind == "set4" {
        let url = job.approved_three_quarter_url.as_deref().ok_or_else(|| anyhow!("set4 job without approved three-quarter"))?;
        images.push(panel.download(url).context("approved download")?);
    }
    // the pickup-point photo goes last and only to OpenAI, not into the review sheet
    let background = pick_background(&backgrounds_dir()?, job.id);
    let mut sent = images.clone();
    if let Some(bg) = &background {
        sent.push(std::fs::read(bg).with_context(|| format!("cannot read {}", bg.display()))?);
    }
    let out = ai.edit(model, &prompt::build_prompt(job, background.is_some()), &sent)?;
    let cost = openai::cost_usd(&out.usage, pin, pout);
    // paid now: keep the picture on disk until the panel has it
    if let Err(e) = pending::save(pdir, job.id, &out.png, cost) {
        eprintln!("job {}: could not save pending copy: {e:#}", job.id);
    }
    match pending::with_retries(&RETRY_DELAYS, api::is_client_error, || panel.result(job.id, &out.png, cost)) {
        Ok(()) => {
            if let Some(p) = pending::list(pdir).into_iter().find(|p| p.meta.job_id == job.id) {
                pending::remove(&p);
            }
            send_sheet(panel, job, &images, &out.png);
            Ok(cost)
        }
        Err(e) => Err(Paid(cost, e.context("result upload")).into()),
    }
}

/// Review sheet for the set (reference, approved three-quarter, new frame); a failure only logs.
fn send_sheet(panel: &api::Panel, job: &prompt::Job, images: &[Vec<u8>], new_png: &[u8]) {
    let mut items: Vec<(String, image::DynamicImage)> = Vec::new();
    for (label, bytes) in [("Эталон", images.first().map(Vec::as_slice)), ("Одобренный", images.get(1).map(Vec::as_slice)), ("Новый кадр", Some(new_png))] {
        match bytes.and_then(|b| image::load_from_memory(b).ok()) {
            Some(img) => items.push((label.to_string(), img)),
            None if label == "Новый кадр" => return,
            None => {}
        }
    }
    let res = variants::to_jpeg(&sheet::preview_sheet(&items), 85)
        .map_err(anyhow::Error::msg)
        .and_then(|jpg| panel.review_sheet(&job.set_key, &jpg));
    if let Err(e) = res {
        eprintln!("job {}: review sheet not sent: {}", job.id, api::redact(&format!("{e:#}")));
    }
}

fn arg<'a>(args: &'a [String], name: &str) -> Option<&'a str> {
    args.iter().position(|a| a == name).and_then(|i| args.get(i + 1)).map(String::as_str)
}

/// Local trial with a pickup-point background: files in, PNG out; the panel is not involved.
fn trial(args: &[String]) -> Result<()> {
    let need = |n: &str| arg(args, n).ok_or_else(|| anyhow!("{n} is required"));
    let job = prompt::Job {
        id: 0,
        kind: arg(args, "--kind").unwrap_or("three_quarter").into(),
        set_key: String::new(),
        brand: need("--brand")?.into(),
        model: need("--model")?.into(),
        season: arg(args, "--season").unwrap_or("winter").into(),
        studded: args.iter().any(|a| a == "--studded"),
        size_class: arg(args, "--class").unwrap_or("r15_18").into(),
        size_hint: arg(args, "--size").unwrap_or("205/55 R16").into(),
        ref_url: String::new(),
        approved_three_quarter_url: None,
    };
    let read = |n: &str| -> Result<Vec<u8>> { std::fs::read(need(n)?).with_context(|| format!("cannot read {n}")) };
    let mut images = vec![read("--ref")?];
    if job.kind == "set4" {
        images.push(read("--approved")?);
    }
    images.push(read("--background")?);
    let ai = openai::OpenAi::new(&home_file(".config/openai/key")?, None)?;
    let model = std::env::var("TIRES_IMAGE_MODEL").unwrap_or_else(|_| "gpt-image-2.5-flare".into());
    let out = ai.edit(&model, &prompt::build_prompt(&job, true), &images)?;
    let cost = openai::cost_usd(&out.usage, env_f64("TIRES_PRICE_IN", 10.0), env_f64("TIRES_PRICE_OUT", 40.0));
    std::fs::write(need("--out")?, &out.png)?;
    println!("saved {}, ${:.4}", need("--out")?, cost);
    Ok(())
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let res = match args.first().map(String::as_str) {
        Some("generate") => {
            let limit = match args.iter().position(|a| a == "--limit") {
                Some(i) => args.get(i + 1).and_then(|v| v.parse().ok()).ok_or_else(|| anyhow!("--limit needs a number")),
                None => Ok(1),
            };
            limit.and_then(generate)
        }
        Some("copies") => {
            let demo = args.iter().position(|a| a == "--demo").and_then(|i| args.get(i + 1));
            match demo {
                Some(dir) => copies::demo(Path::new(dir)),
                None => copies::run(),
            }
        }
        Some("trial") => trial(&args),
        Some("tick") => tick(&args),
        _ => Err(anyhow!("usage: tire-photo-agent tick [--stdin-secrets] | generate [--limit N] | copies [--demo OUT_DIR] | trial --ref F --background F --out F --brand B --model M [--kind set4 --approved F]")),
    };
    if let Err(e) = res {
        eprintln!("error: {}", api::redact(&format!("{e:#}")));
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader, Read as _, Write as _};
    use std::net::TcpListener;
    use std::sync::{Arc, Mutex};

    /// A fake panel on 127.0.0.1: answers by path, records "METHOD path body" of every request.
    fn fake_panel(answers: Vec<(&'static str, &'static str)>) -> (String, Arc<Mutex<Vec<String>>>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let log = Arc::new(Mutex::new(Vec::new()));
        let seen = log.clone();
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let mut stream = stream.unwrap();
                let mut reader = BufReader::new(stream.try_clone().unwrap());
                let mut line = String::new();
                reader.read_line(&mut line).unwrap();
                let mut len = 0;
                loop {
                    let mut h = String::new();
                    reader.read_line(&mut h).unwrap();
                    if h.trim().is_empty() {
                        break;
                    }
                    if let Some(v) = h.to_ascii_lowercase().strip_prefix("content-length:") {
                        len = v.trim().parse().unwrap();
                    }
                }
                let mut body = vec![0; len];
                reader.read_exact(&mut body).unwrap();
                let mut parts = line.split_whitespace();
                let (method, path) = (parts.next().unwrap().to_string(), parts.next().unwrap().to_string());
                seen.lock().unwrap().push(format!("{method} {path} {}", String::from_utf8_lossy(&body)));
                let answer = answers.iter().find(|(p, _)| path.starts_with(p)).map(|(_, a)| *a).unwrap_or("{}");
                let _ = write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{answer}", answer.len());
            }
        });
        (base, log)
    }

    fn ai() -> openai::OpenAi {
        openai::OpenAi::new("k", None).unwrap()
    }

    #[test]
    fn secrets_from_stdin() {
        let s = read_secrets(r#"{"token":"t","openai_key":"sk-x","proxy":"socks5://127.0.0.1:1080"}"#.as_bytes()).unwrap();
        assert_eq!((s.token.as_str(), s.openai_key.as_str(), s.proxy.as_str()), ("t", "sk-x", "socks5://127.0.0.1:1080"));
        assert_eq!(read_secrets(r#"{"token":"t"}"#.as_bytes()).unwrap().openai_key, "");
        let e = read_secrets("not json sk-SECRET".as_bytes()).err().unwrap().to_string();
        assert!(!e.contains("SECRET"), "{e}");
    }

    fn requests(log: &Arc<Mutex<Vec<String>>>) -> Vec<String> {
        log.lock().unwrap().clone()
    }

    #[test]
    fn tick_without_a_request_only_asks() {
        let (base, log) = fake_panel(vec![("/api/image-jobs/work", r#"{"run":false,"scope":"","request_at":"","copies":false,"copies_at":""}"#)]);
        tick_with(&api::Panel::new(&base, "t"), Some(ai()), Path::new("/nonexistent")).unwrap();
        assert_eq!(requests(&log), vec!["GET /api/image-jobs/work ".to_string()]);
    }

    #[test]
    fn tick_without_a_key_reports_it_and_still_makes_copies() {
        let (base, log) = fake_panel(vec![
            ("/api/image-jobs/work", r#"{"run":true,"scope":"all","request_at":"2026-10-07T11:00:00","copies":false,"copies_at":""}"#),
            ("/api/photo-copies/todo", "[]"),
            ("/api/image-jobs/run-done", r#"{"ok":true}"#),
        ]);
        tick_with(&api::Panel::new(&base, "t"), None, Path::new("/nonexistent")).unwrap();
        let r = requests(&log);
        assert_eq!(r.len(), 3, "{r:?}");
        assert!(r[1].starts_with("GET /api/photo-copies/todo"));
        assert!(r[2].starts_with("POST /api/image-jobs/run-done") && r[2].contains("request_at=2026-10-07T11%3A00%3A00") && r[2].contains("done=0"));
        assert!(!r.iter().any(|x| x.contains("/api/image-jobs?")), "no job is taken without a key");
    }

    #[test]
    fn tick_rush_takes_only_rush_jobs_until_none() {
        let (base, log) = fake_panel(vec![
            ("/api/image-jobs/work", r#"{"run":true,"scope":"rush","request_at":"a","copies":true,"copies_at":"b"}"#),
            ("/api/image-jobs?", "[]"),
            ("/api/photo-copies/todo", "[]"),
            ("/api/image-jobs/run-done", r#"{"ok":true}"#),
        ]);
        let pdir = std::env::temp_dir().join(format!("tick-pending-{}", std::process::id()));
        tick_with(&api::Panel::new(&base, "t"), Some(ai()), &pdir).unwrap();
        let r = requests(&log);
        assert!(r[1].starts_with("GET /api/image-jobs?limit=1&only=rush"), "{r:?}");
        assert!(r.last().unwrap().contains("copies_at=b") && r.last().unwrap().contains("failed=0"));
    }

    #[test]
    fn background_alternates_and_skips_other_files() {
        let dir = std::env::temp_dir().join(format!("bg-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        assert_eq!(pick_background(&dir, 1), None);
        for f in ["b.jpg", "a.png", "notes.txt"] {
            std::fs::write(dir.join(f), b"x").unwrap();
        }
        assert_eq!(pick_background(&dir, 2), Some(dir.join("a.png")));
        assert_eq!(pick_background(&dir, 3), Some(dir.join("b.jpg")));
        assert_eq!(pick_background(&dir.join("missing"), 3), None);
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
