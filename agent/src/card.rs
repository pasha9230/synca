//! Size cards: darkened background with big Russian text, rendered through SVG (resvg).
use image::imageops::{self, FilterType};
use image::{DynamicImage, GenericImageView, RgbaImage};
use std::sync::{Arc, OnceLock};

pub const CARD_W: u32 = 1024;
pub const CARD_H: u32 = 1536;

pub fn fontdb() -> Arc<usvg::fontdb::Database> {
    static DB: OnceLock<Arc<usvg::fontdb::Database>> = OnceLock::new();
    DB.get_or_init(|| {
        let mut db = usvg::fontdb::Database::new();
        for p in [
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/Library/Fonts/Arial Unicode.ttf",
            "/Library/Fonts/Arial.ttf",
            // the server (Ubuntu, `apt install fonts-liberation`): Liberation Sans has the metrics of Arial
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ] {
            let _ = db.load_font_file(p);
        }
        Arc::new(db)
    })
    .clone()
}

/// A card without a font would go out with no text at all: refuse it instead.
pub fn require_fonts(faces: usize) -> Result<(), String> {
    if faces == 0 {
        return Err("no font for the size cards (on the server: apt install fonts-liberation)".into());
    }
    Ok(())
}

pub fn fonts_loaded() -> usize {
    fontdb().len()
}

/// Render SVG onto a transparent canvas; returns straight (non-premultiplied) RGBA.
pub fn render_svg(svg: &str, w: u32, h: u32) -> Option<RgbaImage> {
    let options = usvg::Options { fontdb: fontdb(), ..Default::default() };
    let tree = usvg::Tree::from_str(svg, &options).ok()?;
    let mut pixmap = tiny_skia::Pixmap::new(w, h)?;
    resvg::render(&tree, tiny_skia::Transform::identity(), &mut pixmap.as_mut());
    let mut out = RgbaImage::new(w, h);
    for (dst, src) in out.pixels_mut().zip(pixmap.pixels()) {
        let c = src.demultiply();
        *dst = image::Rgba([c.red(), c.green(), c.blue(), c.alpha()]);
    }
    Some(out)
}

pub fn blend(base: &mut RgbaImage, over: &RgbaImage, x0: u32, y0: u32) {
    for (x, y, o) in over.enumerate_pixels() {
        let (bx, by) = (x + x0, y + y0);
        if o[3] == 0 || bx >= base.width() || by >= base.height() {
            continue;
        }
        let a = o[3] as f32 / 255.0;
        let b = base.get_pixel_mut(bx, by);
        for c in 0..3 {
            b[c] = (o[c] as f32 * a + b[c] as f32 * (1.0 - a)).round() as u8;
        }
        b[3] = 255;
    }
}

pub fn xml_escape(s: &str) -> String {
    s.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;").replace('"', "&quot;")
}

/// (fill, stroke) by background brightness: light text on dark, dark on light.
pub fn pick_text_palette(img: &DynamicImage) -> (&'static str, &'static str) {
    let px = img.to_luma8();
    let mean = px.pixels().map(|p| p[0] as u64).sum::<u64>() as f32 / (px.width() * px.height()).max(1) as f32;
    if mean < 140.0 { ("#ffffff", "#000000") } else { ("#111111", "#ffffff") }
}

fn fit_size(text: &str, max_px: f32, max_w: f32) -> f32 {
    max_px.min(max_w / (text.chars().count().max(1) as f32 * 0.6))
}

pub fn card_svg(size: &str, season_line: &str, set_line: Option<&str>, fill: &str, stroke: &str) -> String {
    let mut lines = vec![(size, fit_size(size, 190.0, 940.0), 700.0_f32, true), (season_line, fit_size(season_line, 96.0, 900.0), 860.0, false)];
    if let Some(s) = set_line {
        lines.push((s, fit_size(s, 80.0, 900.0), 990.0, false));
    }
    let mut svg = format!("<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"{CARD_W}\" height=\"{CARD_H}\" viewBox=\"0 0 {CARD_W} {CARD_H}\">");
    for (text, fs, y, bold) in lines {
        let weight = if bold { "bold" } else { "normal" };
        svg.push_str(&format!(
            "<text x=\"512\" y=\"{y}\" text-anchor=\"middle\" font-family=\"Arial, Liberation Sans\" font-size=\"{fs:.0}\" font-weight=\"{weight}\" fill=\"{fill}\" stroke=\"{stroke}\" stroke-width=\"{:.1}\" paint-order=\"stroke\">{}</text>",
            fs / 40.0,
            xml_escape(text)
        ));
    }
    svg.push_str("</svg>");
    svg
}

/// 1024x1536 card: background covered to size, blurred and darkened, text centered.
pub fn render_card(bg: &DynamicImage, size: &str, season_line: &str, set_line: Option<&str>) -> DynamicImage {
    let (w, h) = bg.dimensions();
    let scale = (CARD_W as f32 / w as f32).max(CARD_H as f32 / h as f32);
    let (sw, sh) = (((w as f32 * scale).ceil() as u32).max(CARD_W), ((h as f32 * scale).ceil() as u32).max(CARD_H));
    let big = imageops::resize(&bg.to_rgba8(), sw, sh, FilterType::Triangle);
    let cropped = imageops::crop_imm(&big, (sw - CARD_W) / 2, (sh - CARD_H) / 2, CARD_W, CARD_H).to_image();
    let mut base = imageops::blur(&cropped, 14.0);
    for p in base.pixels_mut() {
        for c in 0..3 {
            p[c] = (p[c] as f32 * 0.42) as u8;
        }
        p[3] = 255;
    }
    let (fill, stroke) = pick_text_palette(&DynamicImage::ImageRgba8(base.clone()));
    if let Some(over) = render_svg(&card_svg(size, season_line, set_line, fill, stroke), CARD_W, CARD_H) {
        blend(&mut base, &over, 0, 0);
    }
    DynamicImage::ImageRgba8(base)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bg() -> DynamicImage {
        image::open(concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/three_quarter.jpg")).unwrap()
    }

    /// Count of near-white pixels (text) in a row band.
    fn bright(img: &DynamicImage, y0: u32, y1: u32) -> usize {
        img.to_luma8().enumerate_pixels().filter(|(_, y, p)| *y >= y0 && *y < y1 && p[0] > 200).count()
    }

    #[test]
    fn card_has_size_text() {
        let card = render_card(&bg(), "205/55 R16", "Зима · шипы", Some("Комплект 4 шт."));
        assert_eq!(card.dimensions(), (CARD_W, CARD_H));
        assert!(bright(&card, 540, 720) > 2000, "size text missing");
        assert!(bright(&card, 790, 880) > 500, "season line missing (Cyrillic font?)");
    }

    #[test]
    fn no_font_no_card() {
        assert!(require_fonts(0).is_err());
        assert!(require_fonts(2).is_ok());
        assert!(card_svg("205/55 R16", "Зима", None, "#fff", "#000").contains("Liberation Sans"));
    }

    #[test]
    fn card_without_set_line() {
        assert!(card_svg("205/55 R16", "Зима", None, "#fff", "#000").matches("<text").count() == 2);
        let with = render_card(&bg(), "205/55 R16", "Зима · шипы", Some("Комплект 4 шт."));
        let without = render_card(&bg(), "205/55 R16", "Зима · шипы", None);
        assert!(bright(&with, 920, 1010) > 300);
        assert!(bright(&without, 920, 1010) < 50);
    }
}
