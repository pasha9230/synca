//! How the agent talks HTTP (ureq 3), with the behaviour it had on ureq 2 spelled out instead of left to defaults:
//! no proxy from the environment (HTTP_PROXY, ALL_PROXY...), 30 seconds to connect (120 for a whole panel request or download), at most 4 redirects,
//! a new connection for every request (ureq 3 does not retry a request on a kept connection the server has dropped),
//! and a failure before any request was sent is told apart from a failure in the middle of one.
use std::time::Duration;
use ureq::config::Config;
use ureq::http::Uri;
use ureq::unversioned::resolver::{DefaultResolver, ResolvedSocketAddrs, Resolver};
use ureq::unversioned::transport::{ConnectionDetails, Connector, DefaultConnector, NextTimeout, Transport};

const CONNECT: Duration = Duration::from_secs(30);
/// The whole request to the panel or an image download: a stalled server can no longer hold a run forever.
const PANEL: Duration = Duration::from_secs(120);
/// ureq 2 with its default of 5 followed at most 4 redirects and failed on the fifth; ureq 3 counts the ones it follows.
const REDIRECTS: u32 = 4;

/// The request never left: name resolution, the connection, the proxy or the TLS handshake failed.
/// The text is a fixed label, never the error itself: that may hold the proxy address with its password.
#[derive(Debug)]
struct Unreached(&'static str);
impl std::fmt::Display for Unreached {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.0)
    }
}
impl std::error::Error for Unreached {}

fn tag(e: ureq::Error, label: &'static str) -> ureq::Error {
    match e {
        ureq::Error::Other(b) if b.is::<Unreached>() => ureq::Error::Other(b),
        _ => ureq::Error::Other(Box::new(Unreached(label))),
    }
}

/// The stock connector chain (socks, CONNECT proxy, TCP, TLS); its errors are marked as "never reached".
#[derive(Debug)]
struct Connect(DefaultConnector);
impl Connector<()> for Connect {
    type Out = Box<dyn Transport>;
    fn connect(&self, details: &ConnectionDetails, chained: Option<()>) -> Result<Option<Self::Out>, ureq::Error> {
        self.0.connect(details, chained).map_err(|e| {
            let label = match &e {
                ureq::Error::ConnectProxyFailed(why) if why.contains(" 407") => "Provided proxy credentials are incorrect",
                ureq::Error::ConnectProxyFailed(_) => "Proxy failed to connect",
                _ => "Connection Failed",
            };
            tag(e, label)
        })
    }
}

#[derive(Debug)]
struct Resolve(DefaultResolver);
impl Resolver for Resolve {
    fn resolve(&self, uri: &Uri, config: &Config, timeout: NextTimeout) -> Result<ResolvedSocketAddrs, ureq::Error> {
        self.0.resolve(uri, config, timeout).map_err(|e| tag(e, "Dns Failed"))
    }
}

/// `proxy`: only what the caller gives, the environment is never read. `overall`: the limit for the whole request.
/// `redirects` 0: the 3xx answer itself is returned, nothing is followed.
/// `status_as_error`: 4xx and 5xx come back as `Error::StatusCode` (without the body).
pub fn agent(proxy: Option<ureq::Proxy>, overall: Option<Duration>, redirects: u32, status_as_error: bool) -> ureq::Agent {
    build(proxy, overall, redirects, status_as_error, false)
}

/// `https_only`: a plain `http://` URL is refused before anything is sent.
fn build(proxy: Option<ureq::Proxy>, overall: Option<Duration>, redirects: u32, status_as_error: bool, https_only: bool) -> ureq::Agent {
    let config = ureq::Agent::config_builder()
        .proxy(proxy)
        .https_only(https_only)
        .timeout_connect(Some(CONNECT))
        .timeout_global(overall)
        .max_idle_connections(0)
        .max_redirects(redirects)
        .max_redirects_will_error(redirects > 0)
        .http_status_as_error(status_as_error)
        .build();
    ureq::Agent::with_parts(config, Connect(DefaultConnector::default()), Resolve(DefaultResolver::default()))
}

/// The agent for the panel: direct, the whole request within 120 seconds, 4xx/5xx as errors.
pub fn direct() -> ureq::Agent {
    agent(None, Some(PANEL), REDIRECTS, true)
}

/// The agent for an image download: direct, within 120 seconds, no redirects followed, 4xx/5xx as errors.
pub fn download() -> ureq::Agent {
    agent(None, Some(PANEL), 0, true)
}

/// The agent for OpenAI: the given proxy or none, the whole request within `overall`, https only (the key is never sent
/// in the clear, not even after a redirect); the status is read by the caller.
pub fn through(proxy: Option<ureq::Proxy>, overall: Duration) -> ureq::Agent {
    build(proxy, Some(overall), REDIRECTS, false, true)
}

/// Some(label) when nothing was sent (so nothing could be paid for).
pub fn unreached(e: &ureq::Error) -> Option<&'static str> {
    match e {
        ureq::Error::Other(b) => b.downcast_ref::<Unreached>().map(|u| u.0),
        ureq::Error::HostNotFound => Some("Dns Failed"),
        ureq::Error::ConnectionFailed => Some("Connection Failed"),
        ureq::Error::ConnectProxyFailed(_) => Some("Proxy failed to connect"),
        _ => None,
    }
}

/// A short fixed name of the failure for messages and the journal (the names ureq 2 used); never the error text.
pub fn kind(e: &ureq::Error) -> &'static str {
    if let Some(label) = unreached(e) {
        return label;
    }
    match e {
        ureq::Error::StatusCode(_) => "HTTP status error",
        ureq::Error::BadUri(_) => "Bad URL",
        ureq::Error::TooManyRedirects => "Too Many Redirects",
        ureq::Error::InvalidProxyUrl => "Malformed proxy",
        ureq::Error::Protocol(_) | ureq::Error::Http(_) | ureq::Error::LargeResponseHeader(..) => "Bad Header",
        _ => "Network Error",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::net::TcpListener;

    /// A one-shot local server: answers every connection with `answer`, returns the requests it saw.
    fn serve(answer: &'static str, hits: usize) -> (String, std::thread::JoinHandle<Vec<String>>) {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", l.local_addr().unwrap());
        let h = std::thread::spawn(move || {
            let mut seen = Vec::new();
            for _ in 0..hits {
                let (mut s, _) = l.accept().unwrap();
                let mut buf = [0u8; 4096];
                let n = s.read(&mut buf).unwrap();
                seen.push(String::from_utf8_lossy(&buf[..n]).to_string());
                s.write_all(answer.as_bytes()).unwrap();
            }
            seen
        });
        (url, h)
    }

    /// Run in a child process with proxy variables set (see the next test): this process's environment is not touched.
    #[test]
    #[ignore]
    fn child_with_a_proxy_in_the_environment() {
        assert!(std::env::var("ALL_PROXY").is_ok() && ureq::Proxy::try_from_env().is_some(), "run by the_environment_proxy_is_never_used");
        let (url, h) = serve("HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok", 1);
        let a = direct();
        assert!(a.config().proxy().is_none());
        assert!(through(None, Duration::from_secs(5)).config().proxy().is_none());
        assert!(agent(None, None, 0, true).config().proxy().is_none());
        let body = a.get(&url).call().unwrap().into_body().read_to_string().unwrap();
        assert_eq!(body, "ok");
        assert!(h.join().unwrap()[0].starts_with("GET / HTTP/1.1"));
    }

    #[test]
    fn the_environment_proxy_is_never_used() {
        // a proxy that refuses everything: were it used, the request in the child could not succeed
        let dead = TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
        let mut child = std::process::Command::new(std::env::current_exe().unwrap());
        child.args(["--exact", "net::tests::child_with_a_proxy_in_the_environment", "--ignored"]);
        for name in ["HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"] {
            child.env(name, format!("http://127.0.0.1:{dead}"));
        }
        let out = child.env_remove("NO_PROXY").env_remove("no_proxy").output().unwrap();
        let text = String::from_utf8_lossy(&out.stdout);
        assert!(out.status.success() && text.contains("1 passed"), "{text}");
    }

    #[test]
    fn timeouts_and_redirects_are_the_old_ones() {
        let a = direct();
        assert_eq!(a.config().timeouts().connect, Some(Duration::from_secs(30)));
        assert_eq!(a.config().timeouts().global, Some(Duration::from_secs(120)));
        assert_eq!((a.config().max_redirects(), a.config().max_idle_connections()), (4, 0));
        let d = download();
        assert_eq!(d.config().timeouts().connect, Some(Duration::from_secs(30)));
        assert_eq!(d.config().timeouts().global, Some(Duration::from_secs(120)));
        assert_eq!((d.config().max_redirects(), d.config().max_idle_connections()), (0, 0));
        let o = through(None, Duration::from_secs(300));
        assert_eq!(o.config().timeouts().connect, Some(Duration::from_secs(30)));
        assert_eq!(o.config().timeouts().global, Some(Duration::from_secs(300)));
        assert_eq!((o.config().max_redirects(), o.config().max_idle_connections()), (4, 0));
    }

    #[test]
    fn a_refused_connection_is_unreached_and_a_bad_status_is_not() {
        let port = TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
        let e = direct().get(&format!("http://127.0.0.1:{port}/")).call().err().unwrap();
        assert_eq!(unreached(&e), Some("Connection Failed"));
        assert_eq!(kind(&e), "Connection Failed");

        let (url, h) = serve("HTTP/1.1 500 Oops\r\nContent-Length: 0\r\nConnection: close\r\n\r\n", 1);
        let e = direct().get(&url).call().err().unwrap();
        assert!(matches!(e, ureq::Error::StatusCode(500)));
        assert_eq!(unreached(&e), None);
        h.join().unwrap();
    }

    #[test]
    fn a_cut_answer_is_not_unreached() {
        // the request was sent, the server hung up in the middle of the answer: OpenAI may have charged for it
        let (url, h) = serve("HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nhalf", 1);
        let r = direct().get(&url).call().and_then(|r| r.into_body().read_to_string());
        let e = r.err().unwrap();
        assert_eq!(unreached(&e), None, "{e}");
        assert_eq!(kind(&e), "Network Error");
        h.join().unwrap();
    }

    #[test]
    fn openai_agent_is_https_only_and_the_panel_ones_are_not() {
        assert!(through(None, Duration::from_secs(5)).config().https_only());
        assert!(!direct().config().https_only() && !download().config().https_only());
        // a plain http URL is refused before anything is sent: the listener is never connected to
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        l.set_nonblocking(true).unwrap();
        let url = format!("http://{}/", l.local_addr().unwrap());
        let e = through(None, Duration::from_secs(5)).post(&url).header("Authorization", "Bearer sk-x").send("x").err().unwrap();
        assert!(matches!(e, ureq::Error::RequireHttpsOnly(_)), "{e}");
        assert!(l.accept().is_err());
    }

    #[test]
    fn no_redirects_returns_the_answer_itself() {
        let (url, h) = serve("HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:1/x\r\nContent-Length: 0\r\nConnection: close\r\n\r\n", 1);
        let r = agent(None, None, 0, true).get(&url).call().unwrap();
        assert_eq!(r.status().as_u16(), 302);
        h.join().unwrap();
    }
}
