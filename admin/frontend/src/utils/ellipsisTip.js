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
// 排除布局容器和导航分组（它们的 textContent 包含子元素全部文字，且子元素绝对定位会虚增 scrollWidth 造成误判）
const EXCLUDE_CLASSES = new Set([
  'topnav-dropdown', 'topnav', 'topnav-brand', 'topnav-links',
  'app-layout', 'topnav-content', 'drawer', 'drawer-overlay'
])
function findOverflowTarget(el) {
  let p = el
  while (p && p !== document.body) {
    if (p.classList) {
      for (const c of p.classList) {
        if (EXCLUDE_CLASSES.has(c)) return null
      }
    }
    // 只对有文本内容且真实截断的元素触发（scrollWidth > clientWidth）
    // 且排除 flex/grid 布局容器（它们不会截断文本，scrollWidth 虚高是子元素撑开导致）
    const style = getComputedStyle(p)
    if (style.display === 'flex' || style.display === 'inline-flex' || style.display === 'grid') {
      p = p.parentElement
      continue
    }
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
