import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import { useRef } from 'react'
import { OPEN_MESSAGE_TYPE } from './useFrameOpenLink'
import { frameInputs, hostInput, INPUT_MESSAGE_TYPE, useFrameLinkBridge, webUrlOf } from './useFrameLinkBridge'

const URL_A = 'https://github.com/example-org/repo/pull/7'

/** A host with one frame. The frame's window is a unique object: the hook compares identity only. */
function Host({ frame, open, gesture }: {
  frame: HTMLIFrameElement
  open: (url: string) => void
  gesture?: (frame: HTMLIFrameElement) => boolean
}) {
  const ref = useRef<HTMLIFrameElement | null>(frame)
  useFrameLinkBridge([ref], open, gesture ?? (() => true))
  return null
}

function fakeFrame(): HTMLIFrameElement {
  return { contentWindow: {} as Window, blur: vi.fn() } as unknown as HTMLIFrameElement
}

function post(data: unknown, source: unknown) {
  const event = new MessageEvent('message', { data })
  Object.defineProperty(event, 'source', { value: source })
  act(() => { window.dispatchEvent(event) })
}

beforeEach(() => {
  // No dashboard input in the last activation lifespan unless a test says so.
  hostInput.lastAt = -Infinity
  frameInputs.clear()
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('useFrameLinkBridge', () => {
  it('opens on a real click in its frame, with no dialog', () => {
    const frame = fakeFrame()
    const open = vi.fn()
    render(<Host frame={frame} open={open} />)
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, frame.contentWindow)
    expect(open).toHaveBeenCalledTimes(1)
    expect(open).toHaveBeenCalledWith(URL_A)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('opens nothing without a click in that frame', () => {
    const frame = fakeFrame()
    const open = vi.fn()
    render(<Host frame={frame} open={open} gesture={() => false} />)
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, frame.contentWindow)
    expect(open).not.toHaveBeenCalled()
  })

  it('opens once per click: focus moves to the frame holder, so a page posting again opens nothing', () => {
    const holder = document.createElement('div')
    const frame = document.createElement('iframe')
    frame.tabIndex = 0
    holder.appendChild(frame)
    document.body.appendChild(holder)
    const frameWindow = {} as Window
    Object.defineProperty(frame, 'contentWindow', { configurable: true, value: frameWindow })
    const open = vi.fn()
    try {
      render(<Host frame={frame} open={open} gesture={(f) => document.activeElement === f} />)
      frame.focus()
      expect(document.activeElement).toBe(frame)
      post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, frameWindow)
      post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, frameWindow)
      post({ type: OPEN_MESSAGE_TYPE, url: 'https://example.test/other' }, frameWindow)
      expect(open).toHaveBeenCalledTimes(1)
      // A keyboard user keeps their place: focus is on the frame's holder, one
      // Tab from the page, not dropped on the document body.
      expect(document.activeElement).toBe(holder)
      expect(holder.getAttribute('tabindex')).toBe('-1')
    } finally {
      holder.remove()
    }
  })

  it('ignores a message from a window that is not its frame', () => {
    const frame = fakeFrame()
    const open = vi.fn()
    render(<Host frame={frame} open={open} />)
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, {} as Window)
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, window)
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, null)
    expect(open).not.toHaveBeenCalled()
  })

  it.each([
    'javascript:alert(1)',
    'data:text/html,<p>x</p>',
    'blob:https://example.test/0b1c',
    'file:///etc/hosts',
    'mailto:someone@example.test',
    'vscode://file/x',
    '/relative/path',
    'https://user:pass@example.test/',
    'https://github.com@evil.example/',
  ])('refuses %s', (url) => {
    const frame = fakeFrame()
    const open = vi.fn()
    render(<Host frame={frame} open={open} />)
    post({ type: OPEN_MESSAGE_TYPE, url }, frame.contentWindow)
    expect(open).not.toHaveBeenCalled()
  })

  it('opens the parsed URL, so what the hover title shows is what opens', () => {
    expect(webUrlOf({ type: OPEN_MESSAGE_TYPE, url: 'HTTPS://Example.TEST/a b' })).toBe('https://example.test/a%20b')
    expect(webUrlOf({ type: OPEN_MESSAGE_TYPE, url: 'http://example.test' })).toBe('http://example.test/')
    expect(webUrlOf({ type: 'kirocrew-dashboard:act', url: URL_A })).toBeNull()
    const long = `https://example.test/${'a'.repeat(4096)}`
    expect(webUrlOf({ type: OPEN_MESSAGE_TYPE, url: long })).toBe(long)
  })

  it('opens through window.open with noopener and noreferrer by default', () => {
    const frame = fakeFrame()
    const spy = vi.spyOn(window, 'open').mockImplementation(() => null)
    function DefaultHost() {
      const ref = useRef<HTMLIFrameElement | null>(frame)
      useFrameLinkBridge([ref], undefined, () => true)
      return null
    }
    render(<DefaultHost />)
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, frame.contentWindow)
    expect(spy).toHaveBeenCalledWith(URL_A, '_blank', 'noopener,noreferrer')
  })

  it('records the dashboard\'s own clicks and keys', () => {
    const now = vi.spyOn(performance, 'now').mockReturnValue(1234)
    document.body.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true }))
    expect(hostInput.lastAt).toBe(1234)
    now.mockReturnValue(5678)
    document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }))
    expect(hostInput.lastAt).toBe(5678)
  })

  describe('right after a click or key in a sibling frame', () => {
    // The dashboard sees that click's activation too, and cannot see focus move
    // from one frame to another, so the sibling's report holds this frame's open.
    beforeEach(() => {
      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    })

    it('waits out the sibling\'s input, then opens only if a click in this frame is live', () => {
      const frame = fakeFrame()
      const sibling = {} as Window
      const open = vi.fn()
      let live = true
      render(<Host frame={frame} open={open} gesture={() => live} />)
      post({ type: INPUT_MESSAGE_TYPE }, sibling)
      post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, frame.contentWindow)
      expect(open).not.toHaveBeenCalled()
      act(() => { vi.advanceTimersByTime(6000) })
      expect(open).toHaveBeenCalledTimes(1)

      post({ type: INPUT_MESSAGE_TYPE }, sibling)
      post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, frame.contentWindow)
      live = false
      act(() => { vi.advanceTimersByTime(6000) })
      expect(open).toHaveBeenCalledTimes(1)
    })

    it('drops the request when the sibling reports more input while it waits', () => {
      const frame = fakeFrame()
      const sibling = {} as Window
      const open = vi.fn()
      render(<Host frame={frame} open={open} />)
      post({ type: INPUT_MESSAGE_TYPE }, sibling)
      post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, frame.contentWindow)
      act(() => { vi.advanceTimersByTime(1000) })
      post({ type: INPUT_MESSAGE_TYPE }, sibling)
      act(() => { vi.advanceTimersByTime(10000) })
      expect(open).not.toHaveBeenCalled()
    })

    it('does not hold a frame on its own input', () => {
      const frame = fakeFrame()
      const open = vi.fn()
      render(<Host frame={frame} open={open} />)
      post({ type: INPUT_MESSAGE_TYPE }, frame.contentWindow)
      post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, frame.contentWindow)
      expect(open).toHaveBeenCalledTimes(1)
    })
  })

  describe('right after a click or key in the dashboard', () => {
    // The live activation may be that dashboard input, and a page script can
    // focus its own frame, so the request waits until that input has expired.
    beforeEach(() => {
      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
      hostInput.lastAt = performance.now()
    })

    it('opens nothing at once, and nothing later when no click in the frame is live', () => {
      const frame = fakeFrame()
      const open = vi.fn()
      let live = true
      render(<Host frame={frame} open={open} gesture={() => live} />)
      post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, frame.contentWindow)
      expect(open).not.toHaveBeenCalled()
      live = false
      act(() => { vi.advanceTimersByTime(6000) })
      expect(open).not.toHaveBeenCalled()
    })

    it('opens once the dashboard input has expired if a click in the frame is still live', () => {
      const frame = fakeFrame()
      const open = vi.fn()
      render(<Host frame={frame} open={open} />)
      post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, frame.contentWindow)
      post({ type: OPEN_MESSAGE_TYPE, url: 'https://example.test/other' }, frame.contentWindow)
      act(() => { vi.advanceTimersByTime(4000) })
      expect(open).not.toHaveBeenCalled()
      act(() => { vi.advanceTimersByTime(2000) })
      expect(open).toHaveBeenCalledTimes(1)
      expect(open).toHaveBeenCalledWith(URL_A)
    })

    it('opens a key press 100 ms after a Tab into the frame, before that press expires', () => {
      // Tab at 0 is the dashboard's; Enter at 100 ms is the frame's, live to 5100 ms.
      const frame = fakeFrame()
      const open = vi.fn()
      render(<Host frame={frame} open={open} />)
      post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, frame.contentWindow)
      act(() => { vi.advanceTimersByTime(5090) })
      expect(open).toHaveBeenCalledTimes(1)
    })

    it('drops the request when the dashboard gets another click or key while it waits', () => {
      const frame = fakeFrame()
      const open = vi.fn()
      render(<Host frame={frame} open={open} />)
      post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, frame.contentWindow)
      document.body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
      act(() => { vi.advanceTimersByTime(6000) })
      expect(open).not.toHaveBeenCalled()
    })
  })
})
