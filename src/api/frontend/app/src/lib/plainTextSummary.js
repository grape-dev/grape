// 场景卡片描述摘要：把 market_description 里的 Markdown 标记剥成可读纯文本。
//
// 卡片不渲染 Markdown（避免 XSS 面），只需要一段可读摘要：
//   1. 去掉代码围栏、行内代码反引号、标题/强调/列表/引用等标记；
//   2. 链接与图片保留可见文字，简单 HTML 标签去掉；
//   3. 按空行切段取首个非空段，空白归一；
//   4. 超长优先在句末标点处截断，退化为空白边界，最后按字符兜底并加省略号。

// 默认摘要长度：按卡片「两行（CSS line-clamp: 2）」的可读量级估算，无 spec 数值来源，
// 按 L2 登记为待测量项；调用方可通过 plainTextSummary(text, maxLength) 覆盖。
export const DEFAULT_MAX_LENGTH = 140
const ELLIPSIS = '…'

const HTML_ENTITIES = {
  amp: '&',
  lt: '<',
  gt: '>',
  quot: '"',
  apos: "'",
  nbsp: ' ',
}

function decodeEntities(text) {
  return text.replace(/&(#x[0-9a-fA-F]+|#[0-9]+|[a-zA-Z]+);/g, (raw, body) => {
    if (body[0] === '#') {
      const hex = body[1] === 'x' || body[1] === 'X'
      const code = parseInt(body.slice(hex ? 2 : 1), hex ? 16 : 10)
      if (Number.isFinite(code) && code > 0 && code <= 0x10ffff) {
        try {
          return String.fromCodePoint(code)
        } catch (e) {
          return raw
        }
      }
      return raw
    }
    const key = body.toLowerCase()
    return Object.prototype.hasOwnProperty.call(HTML_ENTITIES, key) ? HTML_ENTITIES[key] : raw
  })
}

function stripCode(text) {
  // 成对围栏整块移除（含内容），未闭合围栏移除到文末
  let out = text.replace(/(^|\n)[ \t]*(`{3,}|~{3,})[^\n]*\n[\s\S]*?\n[ \t]*\2[ \t]*(?=\n|$)/g, '\n')
  out = out.replace(/(^|\n)[ \t]*(`{3,}|~{3,})[^\n]*\n[\s\S]*$/g, '\n')
  // 行内代码只去掉反引号，保留可用文字
  out = out.replace(/(`+)([^`]*?)\1/g, (raw, ticks, inner) => inner.trim())
  return out
}

function stripInlineMarkup(text) {
  let out = text
  // 段内 HTML 换行/段落标签先变成空白，避免前后文粘连
  out = out.replace(/<br\s*\/?>/gi, ' ')
  out = out.replace(/<\/(p|div|li|h[1-6]|blockquote|tr)>/gi, '\n')
  // 自动链接 <https://...> 保留 URL，随后再剥普通标签
  out = out.replace(/<((?:https?:\/\/|mailto:)[^>\s]+)>/gi, '$1')
  out = out.replace(/<[^>]+>/g, '')
  out = decodeEntities(out)

  // 图片/链接：保留 alt 与可见文字
  out = out.replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
  out = out.replace(/!\[([^\]]*)\]\[[^\]]*\]/g, '$1')
  out = out.replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
  out = out.replace(/\[([^\]]+)\]\[[^\]]*\]/g, '$1')

  // 强调/删除线（先长后短）
  out = out.replace(/\*\*\*([^*\n]+)\*\*\*/g, '$1')
  out = out.replace(/\*\*([^*\n]+)\*\*/g, '$1')
  out = out.replace(/__([^_\n]+)__/g, '$1')
  out = out.replace(/~~([^~\n]+)~~/g, '$1')
  out = out.replace(/(^|[^A-Za-z0-9])_([^_\n]+)_(?=[^A-Za-z0-9]|$)/g, '$1$2')
  out = out.replace(/\*([^*\n\s](?:[^*\n]*[^*\n\s])?)\*/g, '$1')

  return out
}

function stripBlockMarkers(text) {
  return text
    .replace(/^[ \t]*(?:>[ \t]?)+/gm, '')
    .replace(/^[ \t]*#{1,6}[ \t]*/gm, '')
    .replace(/^[ \t]*(?:[-*+]|\d{1,3}[.)])[ \t]+/gm, '')
    .replace(/^\[[ xX]\][ \t]+/gm, '')
    .replace(/^[ \t]*(?:=+|-{2,}|\*{3,}|_{3,})[ \t]*$/gm, '')
}

export function stripMarkdown(input) {
  if (input === null || input === undefined) return ''
  let text = String(input).replace(/\r\n?/g, '\n')
  text = stripCode(text)
  text = stripBlockMarkers(text)
  text = stripInlineMarkup(text)
  return text
}

function normalizeParagraph(paragraph) {
  return paragraph
    .split('\n')
    .map((line) => line.replace(/\s+/g, ' ').trim())
    .filter(Boolean)
    .join(' ')
}

export function firstParagraph(input) {
  const stripped = stripMarkdown(input)
  if (!stripped.trim()) return ''
  const paragraphs = stripped.split(/\n[ \t]*\n+/)
  for (const paragraph of paragraphs) {
    const normalized = normalizeParagraph(paragraph)
    if (normalized) return normalized
  }
  return ''
}

function trimTrailingNoise(text) {
  return text.replace(/[\s*_~`#>|\-—–、，,：:；;]+$/u, '').trim()
}

export function truncateSummary(text, maxLength = DEFAULT_MAX_LENGTH) {
  if (!text) return ''
  if (!Number.isFinite(maxLength) || maxLength <= 0) return text
  const chars = Array.from(text)
  if (chars.length <= maxLength) return text

  let cut = chars.slice(0, maxLength).join('')
  const minBoundary = Math.floor(maxLength * 0.6)

  // 优先在句末标点处收口，避免把一句话截成半句
  let boundary = -1
  const punct = /[。！？.!?；;]/g
  let match
  while ((match = punct.exec(cut)) !== null) {
    boundary = match.index + match[0].length
  }
  if (boundary >= minBoundary) {
    cut = cut.slice(0, boundary)
  } else {
    let space = -1
    for (let i = cut.length - 1; i >= minBoundary; i -= 1) {
      if (/\s/.test(cut[i])) {
        space = i
        break
      }
    }
    if (space > 0) cut = cut.slice(0, space)
  }

  const cleaned = trimTrailingNoise(cut)
  if (!cleaned) return chars.slice(0, maxLength).join('').trim() + ELLIPSIS
  return cleaned + ELLIPSIS
}

export function plainTextSummary(input, maxLength = DEFAULT_MAX_LENGTH) {
  const paragraph = firstParagraph(input)
  if (!paragraph) return ''
  return truncateSummary(paragraph, maxLength)
}