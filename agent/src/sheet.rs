//! Review sheet: labelled frames side by side, sent to the panel (-> Telegram).
use crate::card::{blend, render_svg, xml_escape};
use image::imageops::{self, FilterType};
use image::{DynamicImage, GenericImageView, Rgba, RgbaImage};

const CELL_W: u32 = 360;
const CELL_H: u32 = 540;
const LABEL_H: u32 = 44;
const GAP: u32 = 12;

pub fn preview_sheet(items: &[(String, DynamicImage)]) -> DynamicImage {
    let n = items.len().max(1) as u32;
    let (w, h) = (n * CELL_W + (n + 1) * GAP, CELL_H + LABEL_H + 2 * GAP);
    let mut sheet = RgbaImage::from_pixel(w, h, Rgba([255, 255, 255, 255]));
    for (i, (label, img)) in items.iter().enumerate() {
        let x = GAP + i as u32 * (CELL_W + GAP);
        let (iw, ih) = img.dimensions();
        let s = (CELL_W as f32 / iw as f32).min(CELL_H as f32 / ih as f32);
        let (tw, th) = (((iw as f32 * s) as u32).max(1), ((ih as f32 * s) as u32).max(1));
        let thumb = imageops::resize(&img.to_rgba8(), tw, th, FilterType::Triangle);
        imageops::replace(&mut sheet, &thumb, (x + (CELL_W - tw) / 2) as i64, (GAP + (CELL_H - th) / 2) as i64);
        let svg = format!(
            "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"{CELL_W}\" height=\"{LABEL_H}\"><text x=\"{}\" y=\"30\" text-anchor=\"middle\" font-family=\"Arial\" font-size=\"26\" fill=\"#111\">{}</text></svg>",
            CELL_W / 2,
            xml_escape(label)
        );
        if let Some(over) = render_svg(&svg, CELL_W, LABEL_H) {
            blend(&mut sheet, &over, x, GAP + CELL_H);
        }
    }
    DynamicImage::ImageRgba8(sheet)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sheet_holds_all_items() {
        let a = DynamicImage::ImageRgba8(RgbaImage::from_pixel(200, 300, Rgba([200, 0, 0, 255])));
        let b = DynamicImage::ImageRgba8(RgbaImage::from_pixel(300, 300, Rgba([0, 0, 200, 255])));
        let s = preview_sheet(&[("Эталон".into(), a), ("Кадр".into(), b)]);
        assert_eq!(s.width(), 2 * CELL_W + 3 * GAP);
        let px = s.to_rgba8();
        assert_eq!(px.get_pixel(GAP + CELL_W / 2, GAP + CELL_H / 2)[0], 200);
        assert_eq!(px.get_pixel(2 * GAP + CELL_W + CELL_W / 2, GAP + CELL_H / 2)[2], 200);
        // label text drew some dark pixels
        assert!((GAP + CELL_H..GAP + CELL_H + LABEL_H).any(|y| (0..CELL_W).any(|x| px.get_pixel(GAP + x, y)[0] < 100)));
    }
}
