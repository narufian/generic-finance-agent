import { useChat } from '@ai-sdk/react';
import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import Markdown from 'react-markdown';

const SUGGESTIONS = [
  'Explain EBITDA vs. free cash flow in plain terms',
  'What makes a covenant-lite loan risky?',
  'How do I sanity-check a CIM’s growth claims?',
];

export function App() {
  const { messages, sendMessage, status, stop, error, setMessages, clearError } = useChat();
  const [input, setInput] = useState('');
  const endRef = useRef<HTMLDivElement>(null);
  const busy = status === 'submitted' || status === 'streaming';

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end' });
  }, [messages, status]);

  function send(text: string) {
    const trimmed = text.trim();
    if (!trimmed || busy) return;
    clearError();
    void sendMessage({ text: trimmed });
    setInput('');
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    send(input);
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      send(input);
    }
  }

  return (
    <div className="shell">
      <header className="bar">
        <a className="brand" href="/">
          <span className="mark">F</span>
          <span className="name">Finance Agent</span>
          <span className="sub">Chat</span>
        </a>
        <button className="ghost" onClick={() => setMessages([])} disabled={busy || messages.length === 0}>
          New chat
        </button>
      </header>

      <main className="log" aria-live="polite">
        {messages.length === 0 && (
          <div className="empty">
            <h1>What’s on your mind?</h1>
            <div className="chips">
              {SUGGESTIONS.map((s) => (
                <button key={s} className="chip" onClick={() => send(s)}>
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map((m) => (
          <div key={m.id} className={`msg ${m.role}`}>
            {m.parts.map((part, i) =>
              part.type === 'text' ? (
                m.role === 'assistant' ? (
                  <div key={i} className="md">
                    <Markdown>{part.text}</Markdown>
                  </div>
                ) : (
                  <p key={i}>{part.text}</p>
                )
              ) : null,
            )}
          </div>
        ))}

        {status === 'submitted' && <div className="msg assistant typing">Thinking…</div>}
        {error && <div className="error">Something went wrong: {error.message}</div>}
        <div ref={endRef} />
      </main>

      <form className="composer" onSubmit={onSubmit}>
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Ask anything…"
          rows={1}
          autoFocus
        />
        {busy ? (
          <button type="button" className="primary" onClick={() => void stop()}>
            Stop
          </button>
        ) : (
          <button type="submit" className="primary" disabled={!input.trim()}>
            Send
          </button>
        )}
      </form>
    </div>
  );
}
