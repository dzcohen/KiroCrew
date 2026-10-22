import { useCallback, useEffect, useRef, type RefObject } from 'react'

import { safeHttpUrl } from '../lib/safeUrl'
import { ACTIVATION_LIFESPAN_MS, ACTIVATION_SETTLE_MS } from '../lib/widgetSrcdoc'
import { hasGesture, OPEN_MESSAGE_TYPE, openExternal, useFrameOpenLink } from './useFrameOpenLink'

/**
 * The dashboard's own last click or key press (`performance.now()`).
 *
 * `hasGesture` sees a live activation and the frame focused, but a live
 * activation also follows a click or key in the dashboard, and a page's script
 * can focus its own frame: a click on an artifact row followed by the page
 * focusing itself would pass. Input inside an agent-page frame never reaches
 * these window listeners (the frame is another origin), so anything recorded is
 * the dashboard's own. Starts at load time, not never: this module can load just
 * after the click that opened the page.
 */
export const hostInput = { lastAt: typeof performance === 'undefined' ? 0 : performance.now() }
if (typeof window !== 'undefined') {
  for (const type of ['keydown', 'mousedown', 'pointerdown', 'pointerup', 'touchend']) {
    window.addEventListener(type, () => { hostInput.lastAt = performance.now() }, { capture: true, passive: true })
  }
}

/** The message a bridged frame's shim posts on each click or key in that frame. */
export const INPUT_MESSAGE_TYPE = 'kirocrew-dashboard:input'

/**
 * The last click or key each bridged frame reported, by its window.
 *
 * A click in one frame gives the dashboard a live activation too, and the host
 * cannot see focus move from one frame to another, so a sibling page that
 * focuses itself would pass `hasGesture` on that click. Each frame's shim
 * reports its own input, and a frame's open waits out every OTHER frame's, as it
 * waits out the dashboard's. A report never opens anything, so a page that sends
 * false ones can only delay its siblings' links.
 */
export const frameInputs = new Map<MessageEventSource, number>()
if (typeof window !== 'undefined') {
  window.addEventListener('message', (event: MessageEvent) => {
    const data = event.data as { type?: unknown } | null
    if (!event.source || !data || typeof data !== 'object' || data.type !== INPUT_MESSAGE_TYPE) return
    const now = performance.now()
    frameInputs.set(event.source, now)
    for (const [source, at] of frameInputs) {
      if (now - at > 2 * (ACTIVATION_LIFESPAN_MS + ACTIVATION_SETTLE_MS)) frameInputs.delete(source)
    }
  })
}

/** The latest input that is not the asking frame's own: the dashboard's or a sibling frame's. */
function foreignInputAt(asker: MessageEventSource | null): number {
  let at = hostInput.lastAt
  for (const [source, t] of frameInputs) if (source !== asker && t > at) at = t
  return at
}

/**
 * The web URL an open message asks for, or null when it is not one the link
 * bridge honours.
 *
 * The URL rule is `safeHttpUrl`: only an absolute `http:` or `https:` URL with no
 * user name or password passes, so `javascript:`, `data:`, `blob:`, `file:`,
 * `mailto:` and every custom scheme are refused. The returned value is the PARSED form, so the URL that opens is exactly
 * the one the link's hover title shows.
 */
export function webUrlOf(data: unknown): string | null {
  if (!data || typeof data !== 'object') return null
  const msg = data as { type?: unknown; url?: unknown }
  if (msg.type !== OPEN_MESSAGE_TYPE || typeof msg.url !== 'string') return null
  // The URL rule is the dashboard's one web-link gate (safeHttpUrl); this only
  // normalises what passes it.
  const ok = safeHttpUrl(msg.url)
  return ok ? new URL(ok).href : null
}

/**
 * Move focus from the frame to the element that holds it, so the next open needs
 * a new click in the frame while a keyboard user keeps their place: one Tab goes
 * back into the page. The holder is made focusable from script only
 * (`tabindex="-1"`) when it is not already; with no holder the frame is blurred.
 */
function focusHostOf(frame: HTMLIFrameElement): void {
  const host = frame.parentElement
  if (!host) {
    frame.blur()
    return
  }
  if (!host.hasAttribute('tabindex')) host.setAttribute('tabindex', '-1')
  host.focus({ preventScroll: true })
  if (document.activeElement === frame) frame.blur()
}

/**
 * The host half of the link bridge for frames that hold agent-written pages
 * (saved artifacts, a teammate's shared artifact, chat widgets).
 *
 * Those frames are `sandbox="allow-scripts"` with no popup grant, so a page cannot
 * open a window itself. `buildSrcdoc({ linkBridge: true })` turns a `_blank` or
 * bare web link click, and a `window.open` call, into the same
 * `kirocrew-dashboard:open` message the crew pages use, and gives each web link a
 * hover title with its URL. This hook answers the message, the way a link in a
 * session opens:
 *
 * - only from one of the given frames (`event.source`);
 * - only for a web URL (`webUrlOf`);
 * - only while the person has just clicked or pressed a key in that frame
 *   (`hasGesture`), so a page posting on its own opens nothing;
 * - never on another's input: within an activation's lifespan of a click or key
 *   in the dashboard (`hostInput`: a Tab into the frame, the click that opened
 *   the page) or in another bridged frame (`frameInputs`), the request waits for
 *   that to expire and opens only if an activation is still live with the frame
 *   focused, which is then a click or key in the frame;
 * - once per click: after opening, focus moves to the frame's holder, so a page
 *   that posts again from the same click fails the gesture test until the next
 *   real click, and a keyboard user is one Tab from the page. One request waits
 *   at a time.
 */
export function useFrameLinkBridge(
  frames: ReadonlyArray<RefObject<HTMLIFrameElement | null>>,
  open: (url: string) => void = openExternal,
  gesture: (frame: HTMLIFrameElement) => boolean = hasGesture,
): void {
  const openRef = useRef(open)
  openRef.current = open
  const gestureRef = useRef(gesture)
  gestureRef.current = gesture
  const waiting = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => {
    if (waiting.current) clearTimeout(waiting.current)
  }, [])
  // The frame, URL and gesture checks are useFrameOpenLink's own listener; this
  // hook adds the web-URL rule, the dashboard-input wait and the once-per-click
  // focus move.
  const openOnce = useCallback((url: string, frame?: HTMLIFrameElement) => {
    const go = () => {
      openRef.current(url)
      if (frame) focusHostOf(frame)
    }
    const asker = frame?.contentWindow ?? null
    const seen = foreignInputAt(asker)
    const wait = seen + ACTIVATION_LIFESPAN_MS + ACTIVATION_SETTLE_MS - performance.now()
    if (!(wait > 0)) { go(); return }
    if (waiting.current || !frame) return
    waiting.current = setTimeout(() => {
      waiting.current = null
      if (!(foreignInputAt(asker) > seen) && gestureRef.current(frame)) go()
    }, wait)
  }, [])
  useFrameOpenLink(frames, openOnce, gesture, webUrlOf)
}
