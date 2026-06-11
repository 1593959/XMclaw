// XMclaw MessageErrorBoundary — per-message crash isolation.
// If any message bubble render throws, this catches it and shows
// a compact error indicator instead of crashing the entire chat.

const { h, Component } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);

export class MessageErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    console.warn("[xmc] MessageErrorBoundary caught", error, info);
  }

  render() {
    if (this.state.error) {
      return html`
        <div class="msg-error-boundary" role="alert">
          <span class="msg-error-boundary__icon">⚠</span>
          <span class="msg-error-boundary__text">Render error — message hidden</span>
        </div>
      `;
    }
    return this.props.children;
  }
}
