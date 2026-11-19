//! Remote-shell word quoting, exactly matching `force/remote.py`'s `shlex.quote` (SPEC §40, §56.1).

/// Shell-quote like Python's `shlex.quote`: empty -> `''`; a word of only ASCII alphanumerics or
/// `_@%+=:,./-` is safe and returned unchanged; otherwise every `'` becomes `'"'"'` inside outer quotes.
pub fn shlex_quote(s: &str) -> String {
    if s.is_empty() {
        return "''".to_string();
    }
    if s.chars().all(is_safe_char) {
        return s.to_string();
    }
    let mut out = String::with_capacity(s.len() + 2);
    out.push('\'');
    for c in s.chars() {
        if c == '\'' {
            // close the single-quote, a double-quoted apostrophe, reopen the single-quote
            out.push_str("'\"'\"'");
        } else {
            out.push(c);
        }
    }
    out.push('\'');
    out
}

/// Python's regex is `[^\w@%+=:,./-]` with re.ASCII, so only ASCII letters/digits/underscore are safe.
fn is_safe_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || matches!(c, '_' | '@' | '%' | '+' | '=' | ':' | ',' | '.' | '/' | '-')
}

/// A remote command word (`remote._remote_word`): a leading `~/` keeps its tilde for the remote shell to
/// expand (the rest of the word is quoted), anything else is quoted in full.
pub fn remote_word(word: &str) -> String {
    if let Some(rest) = word.strip_prefix("~/") {
        if rest.is_empty() {
            "~/".to_string()
        } else {
            format!("~/{}", shlex_quote(rest))
        }
    } else {
        shlex_quote(word)
    }
}

/// The remote command line: each word converted like `_remote_word`, joined with single spaces.
pub fn join_cmd(words: &[String]) -> String {
    words.iter().map(|w| remote_word(w)).collect::<Vec<_>>().join(" ")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn q(s: &str) -> String {
        shlex_quote(s)
    }

    #[test]
    fn empty_becomes_two_quotes() {
        assert_eq!(q(""), "''");
    }

    #[test]
    fn plain_word_is_unchanged() {
        assert_eq!(q("force"), "force");
        assert_eq!(q("aBc_09@%+=:,./-"), "aBc_09@%+=:,./-");
    }

    #[test]
    fn space_is_quoted() {
        assert_eq!(q("a b"), "'a b'");
    }

    #[test]
    fn apostrophe_gets_the_three_part_quote() {
        assert_eq!(q("it's"), "'it'\"'\"'s'");
    }

    #[test]
    fn non_ascii_is_unsafe_and_quoted() {
        // `é` is not ASCII alphanumeric, so Python's shlex.quote quotes it
        assert_eq!(q("é"), "'é'");
    }

    #[test]
    fn dollar_sign_is_quoted() {
        assert_eq!(q("$HOME"), "'$HOME'");
    }

    #[test]
    fn remote_word_keeps_tilde_home() {
        assert_eq!(remote_word("~/.local/bin/force"), "~/.local/bin/force");
        assert_eq!(remote_word("~/my dir"), "~/'my dir'");
        assert_eq!(remote_word("~/"), "~/");
    }

    #[test]
    fn join_cmd_single_spaces() {
        assert_eq!(join_cmd(&["a b".into(), "c".into(), "~/.x".into()]), "'a b' c ~/.x");
    }
}
