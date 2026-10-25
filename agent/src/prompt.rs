use serde::Deserialize;

/// One generation job as served by `GET /api/image-jobs`.
#[derive(Debug, Clone, Deserialize)]
pub struct Job {
    pub id: i64,
    pub kind: String,
    #[allow(dead_code)]
    pub set_key: String,
    pub brand: String,
    pub model: String,
    pub season: String,
    pub studded: bool,
    pub size_class: String,
    pub size_hint: String,
    pub ref_url: String,
    pub approved_three_quarter_url: Option<String>,
}

const BASE_HEAD: &str = "Use the reference image ONLY as the exact appearance of the tire: keep the tread pattern, ";
const BASE_TAIL: &str = "sidewall \
design exactly as in the reference; keep any coloured marking lines, the brand logo and the model name lettering exactly as \
in the reference. Do NOT show any tire size, load or speed index, DOT code or other numbers on the sidewall: those areas \
are plain smooth rubber with no readable characters (one photo serves every size of this model). \
The tires are brand-new and NOT mounted on rims (no wheel, empty center hole). \
Make it look like a casual real photo taken on a smartphone by a tire shop employee: natural daylight from a \
warehouse gate, slight perspective, mild sensor noise, realistic rubber texture with fine mold flash hairs on the \
sidewall, true black rubber, no CGI look, no studio backdrop, no text overlays, no watermarks. Vertical 2:3 frame. ";

fn tire_desc(job: &Job) -> String {
    let season = match job.season.as_str() {
        "winter" => "winter",
        "summer" => "summer",
        _ => "all-season",
    };
    let studs = if job.season == "winter" && job.studded {
        "studded, metal studs visible"
    } else {
        "no studs"
    };
    // The size only sets the proportions; it must never be written on the tire.
    let profile = if job.size_class == "r19p" { ", low profile sidewall" } else { "" };
    format!(
        "{} {}, {} tire, {}, proportions of a {} tire{}",
        job.brand, job.model, season, studs, job.size_hint, profile
    )
}

fn season_part(job: &Job) -> &'static str {
    match job.season.as_str() {
        "winter" => "",
        _ => "No winter or snow details: dry floor, no snow, no ice, no snowflake symbols emphasised. ",
    }
}

const LOCATION_TAIL: &str = "The LAST reference image is a real photo of our tire shop: recreate this place behind the \
tire (same ground or floor surface, walls, colours, tire stacks and light) as if the photo was taken right there. \
Background softly out of focus: no readable text on stickers, signs or containers, no cars, no license plates, no people. ";

/// Full prompt: shared part + shot part + season part.
/// With `location`, the last image sent is a photo of the pickup point and replaces the generic scene.
pub fn build_prompt(job: &Job, location: bool) -> String {
    let tire = tire_desc(job);
    let shot = if location {
        let pose = if job.kind == "set4" {
            "A stack of four identical new tires lying flat, shot from slightly above front. No paper labels or stickers \
on the tires. The second reference image shows the same tire already approved; keep it identical."
        } else {
            "One new tire standing upright, three-quarter view showing both tread and sidewall, shallow depth of field."
        };
        format!("{pose} The tire is {tire}. {LOCATION_TAIL}")
    } else if job.kind == "set4" {
        format!(
            "A stack of four identical new tires ({tire}) lying flat on a grey concrete warehouse floor \
against a light wall, shot from slightly above front. No paper labels or stickers on the tires. \
The second reference image shows the same tire already approved; keep it identical. "
        )
    } else {
        format!(
            "One new tire ({tire}) standing upright, three-quarter view showing both tread and \
sidewall, on a wooden plank floor, wall of stacked tires behind, shallow depth of field. "
        )
    };
    let layout = if job.studded && job.season == "winter" { "stud layout, " } else { "" };
    format!("{BASE_HEAD}{layout}{BASE_TAIL}{shot}{}", season_part(job))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn job() -> Job {
        Job {
            id: 1,
            kind: "three_quarter".into(),
            set_key: "k".into(),
            brand: "Ikon".into(),
            model: "Autograph Ice 9".into(),
            season: "winter".into(),
            studded: true,
            size_class: "r15_18".into(),
            size_hint: "205/55 R16".into(),
            ref_url: "http://p/ref".into(),
            approved_three_quarter_url: None,
        }
    }

    #[test]
    fn prompt_studded_mentions_studs() {
        assert!(build_prompt(&job(), false).contains("metal studs"));
    }

    #[test]
    fn prompt_friction_forbids_studs() {
        let mut j = job();
        j.studded = false;
        let p = build_prompt(&j, false);
        assert!(p.contains("no studs"));
        assert!(!p.contains("metal studs"));
    }

    #[test]
    fn prompt_r19p_uses_size_hint() {
        let mut j = job();
        j.size_class = "r19p".into();
        j.size_hint = "255/40 R20".into();
        let p = build_prompt(&j, false);
        assert!(p.contains("255/40 R20") && p.contains("low profile"));
    }

    #[test]
    fn stud_layout_only_for_studded() {
        assert!(build_prompt(&job(), false).contains("stud layout"));
        let mut j = job();
        j.studded = false;
        assert!(!build_prompt(&j, false).contains("stud layout"));
    }

    #[test]
    fn prompt_set4_no_labels() {
        let mut j = job();
        j.kind = "set4".into();
        assert!(build_prompt(&j, false).contains("No paper labels"));
    }

    #[test]
    fn prompt_summer_has_no_winter_details() {
        let mut j = job();
        j.season = "summer".into();
        j.studded = true;
        let p = build_prompt(&j, false);
        assert!(p.contains("No winter or snow") && p.contains("no studs") && !p.contains("metal studs"));
    }

    #[test]
    fn prompt_keeps_markings_generic_and_frame_2_3() {
        let p = build_prompt(&job(), false);
        assert!(!p.contains("orange"));
        assert!(p.contains("keep any coloured marking lines, the brand logo and the model name lettering"));
        assert!(p.contains("Vertical 2:3 frame") && !p.contains("3:4"));
    }

    #[test]
    fn prompt_forbids_size_markings() {
        let p = build_prompt(&job(), false);
        assert!(p.contains("Do NOT show any tire size, load or speed index"));
        assert!(!p.contains("all sidewall lettering"));
        assert!(!p.contains("size 205") && !p.contains(", size "));
    }

    #[test]
    fn location_replaces_generic_scene_and_hides_text() {
        let p = build_prompt(&job(), true);
        assert!(p.contains("LAST reference image is a real photo of our tire shop"));
        assert!(p.contains("no license plates") && p.contains("no readable text"));
        assert!(!p.contains("wooden plank floor"));
        assert!(p.contains("Do NOT show any tire size"));
        let mut j = job();
        j.kind = "set4".into();
        let p = build_prompt(&j, true);
        assert!(p.contains("No paper labels") && !p.contains("grey concrete warehouse floor"));
    }
}
