//! Glyph tier selection and the RICH codepoint set. Two tiers: `Plain` is byte-identical to today's
//! glyphs; `Rich` uses Nerd Font icons / rounded pill caps / slim line usage bars for terminals that can render
//! them (kitty/ghostty/wezterm). Every RICH codepoint is exactly 1 column wide, so no layout changes.
//!
//! The three binaries take `--glyphs auto|rich|plain`; `auto` re-reads the pane's own-session tmux
//! option `@force_glyphs` at startup, on SIGUSR1 and on the 5 s idle re-check.

/// Glyph rendering tier.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Tier {
    Plain,
    Rich,
}

/// The `--glyphs` argument as parsed by the binaries: `rich`/`plain` fix the tier; `auto` re-queries
/// the `@force_glyphs` tmux option at runtime.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum GlyphArg {
    Auto,
    Fixed(Tier),
}

impl GlyphArg {
    pub fn fixed_tier(&self) -> Option<Tier> {
        match self {
            GlyphArg::Auto => None,
            GlyphArg::Fixed(t) => Some(*t),
        }
    }
    pub fn is_auto(&self) -> bool {
        *self == GlyphArg::Auto
    }
}

/// Parse a `--glyphs` value. `auto`/`rich`/`plain` are valid; anything else is an error (the caller
/// turns it into a usage message, like the other flags).
pub fn parse_flag(s: &str) -> Result<GlyphArg, String> {
    match s {
        "auto" => Ok(GlyphArg::Auto),
        "rich" => Ok(GlyphArg::Fixed(Tier::Rich)),
        "plain" => Ok(GlyphArg::Fixed(Tier::Plain)),
        other => Err(format!("invalid --glyphs value: {}", other)),
    }
}

/// Map a tmux `@force_glyphs` option value to a tier: exactly `rich` -> `Rich`; anything else
/// (including empty / missing / error) -> `Plain`.
pub fn from_option(v: &str) -> Tier {
    if v == "rich" {
        Tier::Rich
    } else {
        Tier::Plain
    }
}

/// The separator after an icon that text follows. kitty (and Ghostty) draw a Nerd Font icon across two
/// cells when a space follows it, at text size; RICH therefore leaves the icon a 2-cell slot plus a gap.
pub fn gap(tier: Tier) -> &'static str {
    match tier {
        Tier::Plain => " ",
        Tier::Rich => "  ",
    }
}

/// Extra columns RICH needs per icon slot (`gap(tier)` minus one space).
pub fn extra(tier: Tier) -> usize {
    match tier {
        Tier::Plain => 0,
        Tier::Rich => 1,
    }
}

/// The RICH codepoint for a PLAIN `↑` (outdated marker).
pub const OUTDATED_RICH: char = '\u{F0AA}';
/// RICH finish-flash check.
pub const CHECK_RICH: char = '\u{F058}';

/// The tier as the env/option string the dialogs expect: `rich` or `plain`.
pub fn tier_str(tier: Tier) -> &'static str {
    match tier {
        Tier::Plain => "plain",
        Tier::Rich => "rich",
    }
}

/// The state glyph in a tier: RICH versions replace the PLAIN char.
pub fn state(tier: Tier, plain: char, rich: char) -> char {
    match tier {
        Tier::Plain => plain,
        Tier::Rich => rich,
    }
}

/// Every RICH glyph must be exactly 1 column wide so nothing ever changes layout.
#[cfg(test)]
fn assert_width_1() {
    for c in [
        '\u{F0F3}', '\u{F058}', '\u{F10C}', '\u{F017}', '\u{F057}', '\u{EA85}', '\u{F110}',
        '\u{F059}', '\u{F0AA}', '\u{F187}', '\u{F067}', '\u{E0B6}', '\u{E0B4}', '\u{EBF3}',
        '\u{F11C}', '\u{F011}', '\u{F040}', '\u{F1F8}', '\u{F002}', '\u{F071}', '━', '╸', '✻', '▣',
        '\u{F118}', // Chuck's face (app::CHUCK_RICH)
    ] {
        assert_eq!(unicode_width::UnicodeWidthChar::width(c), Some(1), "width of U+{:04X} must be 1", c as u32);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_flag_values() {
        assert_eq!(parse_flag("auto"), Ok(GlyphArg::Auto));
        assert_eq!(parse_flag("rich"), Ok(GlyphArg::Fixed(Tier::Rich)));
        assert_eq!(parse_flag("plain"), Ok(GlyphArg::Fixed(Tier::Plain)));
        assert!(parse_flag("bogus").is_err());
        assert!(parse_flag("").is_err());
    }

    #[test]
    fn from_option_values() {
        assert_eq!(from_option("rich"), Tier::Rich);
        assert_eq!(from_option("plain"), Tier::Plain);
        assert_eq!(from_option(""), Tier::Plain);
        assert_eq!(from_option("bogus"), Tier::Plain);
        assert_eq!(from_option("RICH"), Tier::Plain); // case-sensitive
    }

    #[test]
    fn every_rich_glyph_is_one_column() {
        assert_width_1();
    }
}
