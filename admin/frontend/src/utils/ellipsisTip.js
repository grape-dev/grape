// 全局截断提示（P0+P2）：
// hover 到真实截断（scrollWidth > clientWidth）的文本元素时，
// 显示跟随鼠标的自绘气泡展示完整内容（替代/增强原生 title）。
// 事件委托一次注册，覆盖所有表格单元格/会话名/列表项，零模板改动。
let tipEl = null
let current = null          // 当前显示提示的元素
let mouseX = 0
let mouseY = 0

// 交互控件不提示（select/input/button 有原生行为）
const EXCLUDE_TAGS = new Set(['SELECT', 'INPUT', 'TEXTAREA', 'BUTTON', 'OPTION', 'A'])

function ensureTip() {
  if (!tipEl) {
    tipEl = document.createElement('div')
    tipEl.id = 'ellipsis-tip'
    document.body.appendChild(tipEl)
  }
  return tipEl
}

// 元素是否真实截断（内容溢出）
function isOverflowing(el) {
  return el.scrollWidth > el.clientWidth + 1
}

// 从目标元素向上找第一个截断的文本容器（td/span/div 等）
function findOverflowTarget(el) {
  let p = el
  while (p && p !== document.body) {
    if (p.textContent && p.textContent.trim() && isOverflowing(p)) return p
    p = p.parentElement
  }
  return null
}

function positionTip() {
  const tip = ensureTip()
  const w = tip.offsetWidth
  const h = tip.offsetHeight
  let left = mouseX + 12
  let top = mouseY + 14
  if (left + w > window.innerWidth - 8) left = mouseX - w - 12
  if (top + h > window.innerHeight - 8) top = mouseY - h - 14
  tip.style.left = left + 'px'
  tip.style.top = top + 'px'
}

function showTip(el) {
  const tip = ensureTip()
  tip.textContent = el.textContent.trim()
  tip.style.visibility = 'hidden'
  tip.style.display = 'block'
  positionTip()
  tip.style.visibility = 'visible'
}

function hideTip() {
  current = null
  if (tipEl) tipEl.style.display = 'none'
}

export function initEllipsisTip() {
  document.addEventListener('mouseover', (e) => {
    const el = e.target
    if (!(el instanceof Element) || EXCLUDE_TAGS.has(el.tagName)) {
      hideTip()
      return
    }
    const target = findOverflowTarget(el)
    if (target) {
      if (target !== current) {
        current = target
        mouseX = e.clientX
        mouseY = e.clientY
        showTip(target)
      } else {
        mouseX = e.clientX
        mouseY = e.clientY
        positionTip()
      }
    } else {
      hideTip()
    }
  })
  // 跟随鼠标
  document.addEventListener('mousemove', (e) => {
    if (!current) return
    mouseX = e.clientX
    mouseY = e.clientY
    positionTip()
  })
  // 滚动时隐藏（避免提示错位）
  window.addEventListener('scroll', hideTip, true)
}
