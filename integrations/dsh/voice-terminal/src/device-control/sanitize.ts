/**
 * Pure redaction/truncation helpers for everything this plugin shows to the
 * model or writes to a session log: window titles, executable paths, and free
 * text that may carry credentials typed on the remote desktop.
 *
 * 派生自 pureexe/dsh-windows-remote-ssh 固定提交 b640f268c1e1ee1134e6fa5b51f15736b2bab26b。
 * 许可证见 licenses/dsh-windows-remote-ssh-MIT.txt（上游许可）。
 */

/** Control characters stripped from model-visible text. */
const CONTROL_CHARACTERS = new RegExp('[\\x00-\\x08\\x0B\\x0C\\x0E-\\x1F\\x7F]', 'gu')

/** Marker appended when a string is cut to its length budget. */
const TRUNCATION_MARKER = '…'

/** `key=value` / `key: value` pairs shaped like a credential. */
const SECRET_ASSIGNMENT = new RegExp(
  '((?:api[_-]?key|token|secret|password|passwd|pwd)\\s*[=:])\\s*[^\\s"\'<>\\x00-\\x1F\\x7F]+',
  'giu',
)

/** JSON Web Tokens (three base64url segments separated by dots). */
const JWT_PATTERN = new RegExp('\\beyJ[A-Za-z0-9_-]{4,}\\.[A-Za-z0-9_-]{4,}\\.[A-Za-z0-9_-]{4,}\\b', 'gu')

/** `Bearer <token>` credentials. */
const BEARER_PATTERN = new RegExp('(\\bbearer\\s+)[A-Za-z0-9._~+/=-]{8,}', 'giu')

/**
 * Strip control characters, collapse tabs, and truncate to `maxLength`.
 *
 * @param text - the raw string.
 * @param maxLength - maximum returned length, including the truncation marker.
 * @returns the sanitized string.
 */
export function sanitizeText(text: string, maxLength: number): string {
  if (maxLength <= 0) return ''
  const cleaned = text.replace(CONTROL_CHARACTERS, '').replace(/\t+/gu, ' ')
  if (cleaned.length <= maxLength) return cleaned
  return cleaned.slice(0, Math.max(0, maxLength - TRUNCATION_MARKER.length)) + TRUNCATION_MARKER
}

/**
 * Sanitize a filesystem path for display, preserving the tail (the filename
 * side, which is usually the distinguishing part) when truncating.
 *
 * @param filePath - the raw path.
 * @param maxLength - maximum returned length, including the truncation marker.
 * @returns the sanitized path.
 */
export function sanitizePath(filePath: string, maxLength: number): string {
  if (maxLength <= 0) return ''
  const cleaned = filePath.replace(CONTROL_CHARACTERS, '').replace(/\t+/gu, ' ')
  if (cleaned.length <= maxLength) return cleaned
  return TRUNCATION_MARKER + cleaned.slice(cleaned.length - (maxLength - TRUNCATION_MARKER.length))
}

/**
 * Redact credential-shaped fragments before text reaches a log or the model:
 * `key=`/`key:` assignments, JWTs, and bearer tokens.
 *
 * @param text - the raw text.
 * @returns the redacted text.
 */
export function redactSensitive(text: string): string {
  return text
    .replace(SECRET_ASSIGNMENT, '$1[redacted]')
    .replace(JWT_PATTERN, '[redacted]')
    .replace(BEARER_PATTERN, '$1[redacted]')
}

/**
 * The one entry point for model-visible strings: redact, then sanitize.
 *
 * @param text - the raw string.
 * @param maxLength - truncation length.
 * @returns the sanitized, redacted string.
 */
export function sanitizeVisible(text: string, maxLength: number): string {
  return sanitizeText(redactSensitive(text), maxLength)
}
