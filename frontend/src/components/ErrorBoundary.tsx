import { Component, type ErrorInfo, type ReactNode } from 'react'

/**
 * Catches render-time exceptions in the subtree so a crashing tab never
 * blanks the whole Game-Admin shell. Shows the error and offers a reset
 * (Re-mounts via key bump) so the user can try again or switch tabs
 * without a full page reload.
 */
interface State {
  err: Error | null
}

export class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { err: null }

  static getDerivedStateFromError(err: Error): State {
    return { err }
  }

  componentDidCatch(err: Error, info: ErrorInfo) {
    // eslint-disable-next-line no-console
    console.error('[game-admin] tab crashed:', err, info)
  }

  reset = () => this.setState({ err: null })

  render() {
    if (this.state.err) {
      return (
        <div className="ga-placeholder" style={{ textAlign: 'left', maxWidth: 720, margin: '0 auto' }}>
          <h3 style={{ color: 'var(--danger, #f85149)', margin: '0 0 6px 0' }}>Tab error</h3>
          <pre
            style={{
              fontSize: 12,
              background: 'rgba(0,0,0,0.25)',
              padding: 10,
              borderRadius: 6,
              overflow: 'auto',
              margin: '0 0 10px 0',
            }}
          >
            {String(this.state.err.stack || this.state.err.message || this.state.err)}
          </pre>
          <button className="ga-btn" onClick={this.reset}>
            Retry
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
