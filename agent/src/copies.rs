//! `copies`: per-account copies, tread crops and size cards from `/api/photo-copies/todo`.
use crate::{api, card, variants};
use anyhow::{anyhow, Context, Result};
use image::{DynamicImage, GenericImageView};
use serde::Deserialize;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Deserialize)]
pub struct CardText {
    pub size: String,
    pub season_line: String,
    pub set_line: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Todo {
    pub account_id: i64,
    pub source_photo_id: i64,
    pub source_url: String,
    pub kind: String,
    pub variant: String,
    pub card: Option<CardText>,
}

/// `<sha>` from `.../photos/<sha>.<ext>`.
pub fn sha_of_url(url: &str) -> Option<String> {
    let name = url.split(['?', '#']).next()?.rsplit('/').next()?;
    let stem = name.rsplit_once('.').map_or(name, |(s, _)| s);
    (!stem.is_empty()).then(|| stem.to_string())
}

/// Build the picture for one todo item; `others` holds hashes of the original and sibling copies (updated for copies).
fn make(t: &Todo, img: &DynamicImage, sha: &str, others: &mut Vec<String>) -> Result<DynamicImage> {
    let seed = variants::seed_for(t.account_id, sha, 0);
    match t.kind.as_str() {
        "copy" => {
            let c = variants::distinct_copy(img, others, t.account_id, sha, variants::MIN_DISTANCE, variants::MAX_TRIES).map_err(anyhow::Error::msg)?;
            others.push(variants::ahash(&c));
            Ok(c)
        }
        "crop" => Ok(variants::tread_crop(img, seed)),
        "card" => {
            let c = t.card.as_ref().ok_or_else(|| anyhow!("card item without card text"))?;
            card::require_fonts(card::fonts_loaded()).map_err(anyhow::Error::msg)?;
            Ok(card::render_card(&variants::copy_variant(img, seed), &c.size, &c.season_line, c.set_line.as_deref()))
        }
        other => Err(anyhow!("unknown kind {other}")),
    }
}

pub fn run() -> Result<()> {
    let panel_url = std::env::var("TIRES_PANEL_URL").map_err(|_| anyhow!("TIRES_PANEL_URL is not set"))?;
    let home = std::env::var("HOME").context("HOME is not set")?;
    let token = std::fs::read_to_string(PathBuf::from(home).join(".config/tires/agent-token")).context("cannot read agent token")?;
    run_with(&api::Panel::new(&panel_url, &token))
}

/// Make and upload every copy, crop and card the panel lists; an error when any item failed.
pub fn run_with(panel: &api::Panel) -> Result<()> {
    let todo = panel.copies_todo()?;
    println!("todo: {}", todo.len());
    // siblings: all copies of one source are made in this run, so they can be compared with each other
    let mut groups: BTreeMap<i64, Vec<Todo>> = BTreeMap::new();
    for t in todo {
        groups.entry(t.source_photo_id).or_default().push(t);
    }
    let (mut ok, mut failed) = (0, 0);
    for (source_id, items) in groups {
        let loaded = (|| -> Result<(DynamicImage, String)> {
            let url = &items[0].source_url;
            if !panel.is_panel_url(url) {
                anyhow::bail!("source_url host differs from TIRES_PANEL_URL");
            }
            let sha = sha_of_url(url).ok_or_else(|| anyhow!("no sha in source_url"))?;
            Ok((image::load_from_memory(&panel.download(url)?)?, sha))
        })();
        let (img, sha) = match loaded {
            Ok(v) => v,
            Err(e) => {
                eprintln!("source {source_id}: {}", api::redact(&format!("{e:#}")));
                failed += items.len();
                continue;
            }
        };
        let mut others = vec![variants::ahash(&img)];
        for t in items {
            let res = make(&t, &img, &sha, &mut others).and_then(|pic| {
                let q = variants::jpeg_quality(variants::seed_for(t.account_id, &sha, 0));
                let jpg = variants::to_jpeg(&pic, q).map_err(anyhow::Error::msg)?;
                panel.upload_copy(&t, &jpg)
            });
            match res {
                Ok(()) => ok += 1,
                Err(e) => {
                    failed += 1;
                    eprintln!("source {source_id} account {} {} {}: {}", t.account_id, t.kind, t.variant, api::redact(&format!("{e:#}")));
                }
            }
        }
    }
    println!("copies uploaded: {ok}, failed: {failed}");
    if failed > 0 {
        anyhow::bail!("{failed} item(s) failed");
    }
    Ok(())
}

/// `copies --demo DIR`: no network; reads DIR/source.jpg or the test fixture and writes 3 copies and 2 cards into DIR.
pub fn demo(dir: &Path) -> Result<()> {
    std::fs::create_dir_all(dir)?;
    let src = [dir.join("source.jpg"), PathBuf::from(concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/three_quarter.jpg"))]
        .into_iter()
        .find(|p| p.exists())
        .ok_or_else(|| anyhow!("no source image"))?;
    let img = image::open(&src)?;
    println!("source {} {:?}", src.display(), img.dimensions());
    let mut others = vec![variants::ahash(&img)];
    for acct in 2..=4 {
        let c = variants::distinct_copy(&img, &others, acct, "demo", variants::MIN_DISTANCE, variants::MAX_TRIES).map_err(anyhow::Error::msg)?;
        others.push(variants::ahash(&c));
        std::fs::write(dir.join(format!("copy_account{acct}.jpg")), variants::to_jpeg(&c, 90).map_err(anyhow::Error::msg)?)?;
        std::fs::write(dir.join(format!("crop_account{acct}.jpg")), variants::to_jpeg(&variants::tread_crop(&img, variants::seed_for(acct, "demo", 0)), 90).map_err(anyhow::Error::msg)?)?;
    }
    let card1 = card::render_card(&img, "205/55 R16", "Зима · шипы", None);
    let card4 = card::render_card(&img, "205/55 R16", "Зима · шипы", Some("Комплект 4 шт."));
    std::fs::write(dir.join("card_x1.jpg"), variants::to_jpeg(&card1, 90).map_err(anyhow::Error::msg)?)?;
    std::fs::write(dir.join("card_x4.jpg"), variants::to_jpeg(&card4, 90).map_err(anyhow::Error::msg)?)?;
    let sheet = crate::sheet::preview_sheet(&[("Оригинал".into(), img.clone()), ("Копия 2".into(), image::open(dir.join("copy_account2.jpg"))?), ("Копия 3".into(), image::open(dir.join("copy_account3.jpg"))?), ("Карточка".into(), card4)]);
    std::fs::write(dir.join("sheet.jpg"), variants::to_jpeg(&sheet, 88).map_err(anyhow::Error::msg)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sha_from_url() {
        assert_eq!(sha_of_url("https://p.example/photos/ab12cd.jpg?x=1").as_deref(), Some("ab12cd"));
        assert_eq!(sha_of_url("https://p.example/photos/ab12cd").as_deref(), Some("ab12cd"));
    }

    #[test]
    fn todo_parses_and_makes_items() {
        let json = r#"[{"account_id":2,"source_photo_id":5,"source_url":"https://p/photos/aa.jpg","kind":"card","variant":"v","card":{"size":"205/55 R16","season_line":"Зима · шипы","set_line":null}},
                       {"account_id":3,"source_photo_id":5,"source_url":"https://p/photos/aa.jpg","kind":"copy","variant":"","card":null}]"#;
        let todo: Vec<Todo> = serde_json::from_str(json).unwrap();
        let img = crate::variants::tests::fixture();
        let mut others = vec![variants::ahash(&img)];
        let card = make(&todo[0], &img, "aa", &mut others).unwrap();
        assert_eq!(card.dimensions(), (card::CARD_W, card::CARD_H));
        let copy = make(&todo[1], &img, "aa", &mut others).unwrap();
        assert_eq!(others.len(), 2);
        assert_eq!(copy.dimensions(), img.dimensions());
    }
}
