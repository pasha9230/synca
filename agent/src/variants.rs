//! Subtle per-account copies and tread crops of approved frames (no mirroring, ever).
use image::imageops::{self, FilterType};
use image::{DynamicImage, GenericImageView};
use imageproc::geometric_transformations::{rotate_about_center, Interpolation};
use rand::rngs::StdRng;
use rand::{Rng, SeedableRng};

const HASH_SIZE: u32 = 32;
pub const MIN_DISTANCE: u32 = 6;
pub const MAX_TRIES: u32 = 20;

/// Average hash: 32x32 luma, bit = pixel >= mean.
pub fn ahash(img: &DynamicImage) -> String {
    let small = imageops::resize(img, HASH_SIZE, HASH_SIZE, FilterType::Triangle);
    let luma = DynamicImage::ImageRgba8(small).to_luma8();
    let total: u32 = luma.as_raw().iter().map(|&p| p as u32).sum();
    let avg = total as f32 / (HASH_SIZE * HASH_SIZE) as f32;
    luma.as_raw().iter().map(|&p| if p as f32 >= avg { '1' } else { '0' }).collect()
}

pub fn hamming(a: &str, b: &str) -> u32 {
    if a.len() != b.len() {
        return u32::MAX;
    }
    a.bytes().zip(b.bytes()).filter(|(x, y)| x != y).count() as u32
}

/// FNV-1a over account id, source sha and attempt: a stable seed.
pub fn seed_for(account_id: i64, sha: &str, attempt: u32) -> u64 {
    let mut h: u64 = 0xcbf29ce484222325;
    let mut eat = |bytes: &[u8]| {
        for b in bytes {
            h ^= *b as u64;
            h = h.wrapping_mul(0x100000001b3);
        }
    };
    eat(&account_id.to_le_bytes());
    eat(sha.as_bytes());
    eat(&attempt.to_le_bytes());
    h
}

/// JPEG quality 86..=93 for this seed.
pub fn jpeg_quality(seed: u64) -> u8 {
    86 + ((seed >> 7) % 8) as u8
}

pub fn to_jpeg(img: &DynamicImage, quality: u8) -> Result<Vec<u8>, String> {
    let mut out = Vec::new();
    let enc = image::codecs::jpeg::JpegEncoder::new_with_quality(&mut out, quality);
    img.to_rgb8().write_with_encoder(enc).map_err(|e| format!("jpeg encode: {e}"))?;
    Ok(out)
}

fn gauss(rng: &mut StdRng) -> f32 {
    let (u1, u2): (f32, f32) = (rng.gen_range(1e-6..1.0), rng.gen());
    (-2.0 * u1.ln()).sqrt() * (std::f32::consts::TAU * u2).cos()
}

fn sign(rng: &mut StdRng) -> f32 {
    if rng.gen_bool(0.5) { 1.0 } else { -1.0 }
}

/// Brightness/contrast +-4 %, per-channel shift +-3, gaussian noise sigma 1.5..3.
fn tone(img: &DynamicImage, rng: &mut StdRng) -> DynamicImage {
    let bright = 1.0 + sign(rng) * rng.gen_range(0.01..0.04_f32);
    let contrast = 1.0 + sign(rng) * rng.gen_range(0.01..0.04_f32);
    let shift: [f32; 3] = [rng.gen_range(-3.0..=3.0), rng.gen_range(-3.0..=3.0), rng.gen_range(-3.0..=3.0)];
    let sigma = rng.gen_range(1.5..3.0_f32);
    let mut px = img.to_rgba8();
    for p in px.pixels_mut() {
        let n = gauss(rng) * sigma;
        for c in 0..3 {
            let v = ((p[c] as f32 - 128.0) * contrast + 128.0) * bright + shift[c] + n;
            p[c] = v.round().clamp(0.0, 255.0) as u8;
        }
    }
    DynamicImage::ImageRgba8(px)
}

/// Rotate +-0.3..1.0 deg, crop 1..4 % per side (never less than the rotation needs), tone, resize back.
pub fn copy_variant(img: &DynamicImage, seed: u64) -> DynamicImage {
    let mut rng = StdRng::seed_from_u64(seed);
    let (w, h) = img.dimensions();
    let angle = sign(&mut rng) * rng.gen_range(0.3..1.0_f32);
    let rgba = img.to_rgba8();
    let edge = *rgba.get_pixel(0, 0);
    let rotated = rotate_about_center(&rgba, angle.to_radians(), Interpolation::Bilinear, edge);
    let (s, c) = (angle.abs().to_radians().sin(), angle.abs().to_radians().cos());
    let need_x = (h as f32 * s / 2.0 + w as f32 * (1.0 - c) / 2.0) / w as f32 + 0.002;
    let need_y = (w as f32 * s / 2.0 + h as f32 * (1.0 - c) / 2.0) / h as f32 + 0.002;
    let side = |need: f32, rng: &mut StdRng| rng.gen_range(0.01..0.04_f32).max(need);
    let (l, r) = (side(need_x, &mut rng), side(need_x, &mut rng));
    let (t, b) = (side(need_y, &mut rng), side(need_y, &mut rng));
    let (x0, y0) = ((w as f32 * l) as u32, (h as f32 * t) as u32);
    let cw = w.saturating_sub(x0 + (w as f32 * r) as u32).max(1);
    let ch = h.saturating_sub(y0 + (h as f32 * b) as u32).max(1);
    let cropped = imageops::crop_imm(&rotated, x0, y0, cw, ch).to_image();
    let back = imageops::resize(&cropped, w, h, FilterType::Triangle);
    tone(&DynamicImage::ImageRgba8(back), &mut rng)
}

/// Copy whose hash differs by `min_distance` from every hash in `others` (original and sibling copies).
pub fn distinct_copy(img: &DynamicImage, others: &[String], account_id: i64, sha: &str, min_distance: u32, max_tries: u32) -> Result<DynamicImage, String> {
    for attempt in 0..max_tries {
        let cand = copy_variant(img, seed_for(account_id, sha, attempt));
        let hash = ahash(&cand);
        if others.iter().all(|o| hamming(&hash, o) >= min_distance) {
            return Ok(cand);
        }
    }
    Err(format!("no distinct copy for account {account_id} in {max_tries} tries"))
}

/// Left half of the frame (the tread), a jittered window per seed, tone-adjusted, scaled to the frame size.
pub fn tread_crop(img: &DynamicImage, seed: u64) -> DynamicImage {
    let mut rng = StdRng::seed_from_u64(seed ^ 0x7ead);
    let (w, h) = img.dimensions();
    let cw = (w as f32 * rng.gen_range(0.44..0.52)) as u32;
    let ch = ((cw as f32 * 1.5) as u32).min(h);
    let x0 = (w as f32 * rng.gen_range(0.0..0.06)) as u32;
    let mid = h as f32 * rng.gen_range(0.45..0.55);
    let y0 = ((mid - ch as f32 / 2.0).max(0.0) as u32).min(h - ch);
    let cropped = imageops::crop_imm(&img.to_rgba8(), x0, y0, cw.min(w - x0), ch).to_image();
    let big = imageops::resize(&cropped, w, h, FilterType::CatmullRom);
    tone(&DynamicImage::ImageRgba8(big), &mut rng)
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    pub(crate) fn fixture() -> DynamicImage {
        image::open(concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/three_quarter.jpg")).unwrap()
    }

    fn bytes(img: &DynamicImage) -> Vec<u8> {
        to_jpeg(img, 90).unwrap()
    }

    #[test]
    fn copy_is_deterministic() {
        let img = fixture();
        assert_eq!(bytes(&copy_variant(&img, 42)), bytes(&copy_variant(&img, 42)));
        assert_ne!(bytes(&copy_variant(&img, 42)), bytes(&copy_variant(&img, 43)));
    }

    #[test]
    fn copy_differs_by_hash() {
        let img = fixture();
        let orig = ahash(&img);
        let copy = distinct_copy(&img, &[orig.clone()], 2, "abc", MIN_DISTANCE, MAX_TRIES).unwrap();
        assert!(hamming(&orig, &ahash(&copy)) >= MIN_DISTANCE);
        assert_eq!(copy.dimensions(), img.dimensions());
    }

    #[test]
    fn copies_of_two_accounts_differ() {
        let img = fixture();
        let mut others = vec![ahash(&img)];
        let a = distinct_copy(&img, &others, 2, "abc", MIN_DISTANCE, MAX_TRIES).unwrap();
        others.push(ahash(&a));
        let b = distinct_copy(&img, &others, 3, "abc", MIN_DISTANCE, MAX_TRIES).unwrap();
        assert!(hamming(&ahash(&a), &ahash(&b)) >= MIN_DISTANCE);
        assert_ne!(bytes(&a), bytes(&b));
    }

    fn halves_diff(a: &DynamicImage, b: &DynamicImage) -> f32 {
        let (a, b) = (a.to_luma8(), b.to_luma8());
        let d: u64 = a.pixels().zip(b.pixels()).map(|(x, y)| (x[0] as i32 - y[0] as i32).unsigned_abs() as u64).sum();
        d as f32 / (a.width() * a.height()) as f32
    }

    #[test]
    fn no_mirror() {
        let img = fixture();
        let copy = copy_variant(&img, 7);
        let (w, h) = img.dimensions();
        let half = |i: &DynamicImage, right: bool| DynamicImage::ImageRgba8(imageops::crop_imm(&i.to_rgba8(), if right { w / 2 } else { 0 }, 0, w / 2, h).to_image());
        let left_c = half(&copy, false);
        let same = halves_diff(&left_c, &half(&img, false));
        let flipped = halves_diff(&left_c, &DynamicImage::ImageRgba8(imageops::flip_horizontal(&half(&img, true).to_rgba8())));
        assert!(same < flipped, "same={same} flipped={flipped}");
    }

    #[test]
    fn crop_is_per_seed_and_frame_sized() {
        let img = fixture();
        let a = tread_crop(&img, 1);
        assert_eq!(a.dimensions(), img.dimensions());
        assert_eq!(bytes(&a), bytes(&tread_crop(&img, 1)));
        assert_ne!(bytes(&a), bytes(&tread_crop(&img, 2)));
    }

    #[test]
    fn quality_in_range() {
        for s in 0..200u64 {
            assert!((86..=93).contains(&jpeg_quality(s * 7919)));
        }
    }
}
