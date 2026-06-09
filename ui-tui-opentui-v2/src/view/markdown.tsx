/**
 * Markdown — assistant/reasoning text rendered with the NATIVE renderable, never
 * a hand-rolled parser (spec v4 §7). Uses `<code filetype="markdown" streaming>`
 * (`CodeRenderable`) — opencode's v2 text path (`session-v2.tsx:358` AssistantText)
 * — backed by the same markdown tokenizer + Tree-sitter as `<markdown>`, but it
 * paints reliably (incl. headless): `drawUnstyledText` draws the raw text
 * immediately while highlighting settles, `conceal` hides the `**`/backtick
 * markers, `streaming` feeds incremental deltas.
 *
 * The `SyntaxStyle` is derived from the active theme (no hardcoded styles — §7.5)
 * and cached by theme-object identity, so all text parts share ONE instance and
 * it's rebuilt only when the skin changes (a new `Theme` object).
 */
import { RGBA, SyntaxStyle } from '@opentui/core'

import type { Theme } from '../logic/theme.ts'
import { useTheme } from './theme.tsx'

const FALLBACK = RGBA.fromHex('#E6EDF3')
const HEX6 = /^#[0-9a-fA-F]{6}$/

/** Theme colors are usually hex but may be `ansi256(n)`/`rgb(...)` after light-mode
 *  normalization — only hand hex to RGBA.fromHex, else fall back. */
function rgba(color: string): RGBA {
  return HEX6.test(color) ? RGBA.fromHex(color) : FALLBACK
}

function buildSyntaxStyle(theme: Theme): SyntaxStyle {
  const c = theme.color
  return SyntaxStyle.fromStyles({
    default: { fg: rgba(c.text) },
    'markup.heading': { bold: true, fg: rgba(c.primary) },
    'markup.heading.1': { bold: true, fg: rgba(c.primary) },
    'markup.heading.2': { bold: true, fg: rgba(c.accent) },
    'markup.heading.3': { bold: true, fg: rgba(c.accent) },
    'markup.bold': { bold: true, fg: rgba(c.text) },
    'markup.italic': { fg: rgba(c.text), italic: true },
    'markup.list': { fg: rgba(c.accent) },
    'markup.quote': { fg: rgba(c.muted) },
    'markup.link': { fg: rgba(c.accent) },
    'markup.raw': { fg: rgba(c.label) },
    'markup.raw.block': { fg: rgba(c.label) }
  })
}

let cache: { theme: Theme; style: SyntaxStyle } | undefined
function syntaxStyleFor(theme: Theme): SyntaxStyle {
  if (cache && cache.theme === theme) return cache.style
  const style = buildSyntaxStyle(theme)
  cache = { style, theme }
  return style
}

export function Markdown(props: { text: string; streaming?: boolean; fg?: string }) {
  const theme = useTheme()
  // opencode's v2 text path (session-v2.tsx AssistantText): the markdown engine via
  // <code filetype="markdown" streaming>. `drawUnstyledText={false}` avoids the
  // raw→styled flash per delta (the streaming flicker); `streaming` re-tokenizes
  // incrementally rather than reparsing the whole buffer each repaint. `fg`
  // overrides the base text color (e.g. muted for reasoning bodies — item 6).
  return (
    <code
      filetype="markdown"
      content={props.text}
      syntaxStyle={syntaxStyleFor(theme())}
      streaming={props.streaming ?? false}
      drawUnstyledText={false}
      conceal
      fg={props.fg ?? theme().color.text}
    />
  )
}
