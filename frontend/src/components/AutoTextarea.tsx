import { useEffect, useRef } from 'react'
import type { TextareaHTMLAttributes } from 'react'

/**
 * Textarea that grows with its content — no inner scrollbar. Sets its
 * height to `scrollHeight` whenever the value changes (and once on mount
 * so initial content is sized correctly).
 *
 * Drop-in replacement for `<textarea>` everywhere we want long-form text
 * without the textarea clipping and showing a scroll handle.
 */
export function AutoTextarea(
  props: TextareaHTMLAttributes<HTMLTextAreaElement> & { minRows?: number },
) {
  const { minRows = 1, value, defaultValue, ...rest } = props
  const ref = useRef<HTMLTextAreaElement | null>(null)

  const resize = () => {
    const el = ref.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = el.scrollHeight + 'px'
  }

  useEffect(() => {
    resize()
  }, [value])

  return (
    <textarea
      {...rest}
      ref={ref}
      rows={minRows}
      value={value}
      defaultValue={defaultValue}
      onInput={(e) => {
        resize()
        rest.onInput?.(e)
      }}
      style={{
        ...(rest.style || {}),
        resize: 'none',
        overflow: 'hidden',
      }}
    />
  )
}
