/**
 * Pure classification helpers for `filesystem_pull`: deciding whether a
 * downloaded file's bytes should come back as an image attachment, inline
 * text, or a generic file attachment. No I/O — kept separate from
 * `tools.ts` so this policy is directly unit-testable.
 *
 * 派生自 pureexe/dsh-windows-remote-ssh 固定提交 b640f268c1e1ee1134e6fa5b51f15736b2bab26b。
 * 许可证见 licenses/dsh-windows-remote-ssh-MIT.txt（上游许可）。
 */

import type { ImageMediaType } from '@deepseek-ai/dsh-attachment'

/** One image format signature: the leading bytes and the media type they identify. */
interface ImageSignature {
  mediaType: ImageMediaType
  /** Byte offset the magic bytes start at (0 for every format below). */
  offset: number
  magic: readonly number[]
}

const IMAGE_SIGNATURES: readonly ImageSignature[] = [
  { mediaType: 'image/png', offset: 0, magic: [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a] },
  { mediaType: 'image/jpeg', offset: 0, magic: [0xff, 0xd8, 0xff] },
  { mediaType: 'image/gif', offset: 0, magic: [0x47, 0x49, 0x46, 0x38] }, // "GIF8"
]

/**
 * Identify a known raster image format from its leading bytes.
 *
 * @param data - the file's raw bytes.
 * @returns the media type, or `undefined` when no known signature matches.
 */
export function detectImageMediaType(data: Buffer): ImageMediaType | undefined {
  for (const { mediaType, offset, magic } of IMAGE_SIGNATURES) {
    if (data.length >= offset + magic.length && magic.every((byte, index) => data[offset + index] === byte)) {
      return mediaType
    }
  }
  // WEBP: "RIFF"....\"WEBP\" - the size field in between rules out a fixed offset table entry.
  if (data.length >= 12
    && data[0] === 0x52 && data[1] === 0x49 && data[2] === 0x46 && data[3] === 0x46 // "RIFF"
    && data[8] === 0x57 && data[9] === 0x45 && data[10] === 0x42 && data[11] === 0x50) { // "WEBP"
    return 'image/webp'
  }
  return undefined
}

/**
 * Decide whether bytes are plausibly UTF-8 text worth inlining, rather than
 * binary content that would just come out mangled. Strict: a single invalid
 * UTF-8 sequence anywhere fails this, which is exactly what real binary
 * content reliably produces (valid-but-coincidental full-file UTF-8 in a
 * genuine binary is vanishingly unlikely).
 *
 * @param data - the file's raw bytes.
 * @returns true when the bytes decode as valid UTF-8.
 */
export function looksLikeText(data: Buffer): boolean {
  try {
    new TextDecoder('utf-8', { fatal: true }).decode(data)
    return true
  } catch {
    return false
  }
}
