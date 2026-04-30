import React, { useEffect, useRef, useState } from 'react';
import MessageBubble from './components/Chat/MessageBubble';
import InputArea from './components/Chat/InputArea';
import TypingIndicator from './components/Chat/TypingIndicator';
import './styles/global.css';

const API_ENDPOINT = import.meta.env.VITE_BACKEND_URL
  ? `${import.meta.env.VITE_BACKEND_URL}/invocations`
  : '/invocations';

const SESSION_KEY = 'chat_session_id';
const HISTORY_KEY = 'chat_history';

function getSessionId() {
  const existing = localStorage.getItem(SESSION_KEY);
  if (existing) return existing;
  const created = `session_${Date.now()}`;
  localStorage.setItem(SESSION_KEY, created);
  return created;
}

export default function App() {
  const [messages, setMessages] = useState(() => {
    const saved = localStorage.getItem(HISTORY_KEY);
    if (saved) return JSON.parse(saved);
    const appName = import.meta.env.VITE_APP_TITLE || 'AWS Migration Assistant';
    return [
      {
        role: 'assistant',
        content: `Hello! I'm your **${appName}**.\n\nI can help you plan migration, analyze architecture diagrams, and estimate costs.\n\n*How can I help you today?*`,
      },
    ];
  });
  const [isLoading, setIsLoading] = useState(false);
  const messagesEndRef = useRef(null);
  const sessionIdRef = useRef(getSessionId());

  useEffect(() => {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(messages));
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  async function handleSendMessage(text, image) {
    const userText = text?.trim() || (image ? 'Uploaded architecture image' : '');
    if (!userText || isLoading) return;

    const nextMessages = [...messages, { role: 'user', content: userText }];
    setMessages(nextMessages);
    setIsLoading(true);

    try {
      const body = {
        input: userText,
        context: { session_id: sessionIdRef.current },
      };
      if (image?.payload) body.image_base64 = image.payload;

      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 300000); // 5 min

      const res = await fetch(API_ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      clearTimeout(timeoutId);

      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }

      const data = await res.json();
      const extractAssistantText = (payload) => {
        if (payload == null) return '';
        if (typeof payload === 'string') return payload.trim();

        const direct =
          payload.output ||
          payload.response ||
          payload.message ||
          payload.answer ||
          payload.completion ||
          payload.text;
        if (typeof direct === 'string' && direct.trim()) return direct.trim();

        // Common Lambda proxy shape: { statusCode, body: "..." }
        if (typeof payload.body === 'string' && payload.body.trim()) {
          const bodyText = payload.body.trim();
          try {
            const parsedBody = JSON.parse(bodyText);
            const nested = extractAssistantText(parsedBody);
            if (nested) return nested;
          } catch (_) {
            return bodyText;
          }
        }

        // Common model/tool response shapes
        if (Array.isArray(payload.content) && payload.content.length > 0) {
          const textParts = payload.content
            .map((item) => (typeof item === 'string' ? item : item?.text))
            .filter(Boolean)
            .join('\n')
            .trim();
          if (textParts) return textParts;
        }

        if (Array.isArray(payload.results) && payload.results.length > 0) {
          const nested = extractAssistantText(payload.results[0]);
          if (nested) return nested;
        }

        if (payload.result) {
          const nested = extractAssistantText(payload.result);
          if (nested) return nested;
        }

        const debugText = JSON.stringify(payload);
        return debugText && debugText !== '{}' ? debugText : '';
      };

      const assistantText = extractAssistantText(data) || 'No response received.';

      setMessages((prev) => [...prev, { role: 'assistant', content: String(assistantText) }]);
    } catch (error) {
      const isTimeout = error.name === 'AbortError';
      setMessages((prev) => [
        ...prev,
        {
          role: 'assistant',
          content: isTimeout
            ? 'The request timed out — diagram generation can take up to a few minutes. Please try again.'
            : `Request failed: ${error.message}. Check service logs and API health.`,
        },
      ]);
    } finally {
      setIsLoading(false);
    }
  }

  return (
    <div className="app-container">
      <header className="header glass-card">
        <div className="logo">A</div>
        <div style={{ flex: 1 }}>
          <h1>{import.meta.env.VITE_APP_TITLE || 'AWS Migration Assistant'}</h1>
          <span className="badge">AgentCore Gateway</span>
        </div>
      </header>

      <main className="chat-area">
        <div className="messages-list">
          {messages.map((msg, idx) => (
            <MessageBubble key={idx} role={msg.role} content={msg.content} />
          ))}
          {isLoading && <TypingIndicator />}
          <div ref={messagesEndRef} />
        </div>
      </main>

      <footer className="input-area-wrapper">
        <InputArea onSend={handleSendMessage} isDisabled={isLoading} />
      </footer>

      <style jsx="true">{`
        .app-container {
          display: flex;
          flex-direction: column;
          height: 100vh;
          background: radial-gradient(circle at 50% 10%, #1a1a2e 0%, #0f0f12 60%);
        }

        .header {
          padding: 16px 24px;
          display: flex;
          align-items: center;
          gap: 16px;
          z-index: 10;
          border-bottom: 1px solid var(--glass-border);
        }

        .logo {
          font-size: 18px;
          font-weight: 700;
          background: var(--bg-tertiary);
          width: 40px;
          height: 40px;
          display: flex;
          align-items: center;
          justify-content: center;
          border-radius: 10px;
          color: var(--text-primary);
        }

        .header h1 {
          font-size: 1.1rem;
          font-weight: 600;
          color: var(--text-primary);
        }

        .badge {
          font-size: 0.75rem;
          background: rgba(99, 102, 241, 0.2);
          color: #818cf8;
          padding: 2px 8px;
          border-radius: 4px;
          border: 1px solid rgba(99, 102, 241, 0.3);
        }

        .chat-area {
          flex: 1;
          overflow-y: auto;
          position: relative;
        }

        .messages-list {
          padding: 24px;
          padding-bottom: 140px;
          max-width: 900px;
          margin: 0 auto;
        }

        .input-area-wrapper {
          position: absolute;
          bottom: 24px;
          left: 0;
          right: 0;
          padding: 0 24px;
          z-index: 20;
        }
      `}</style>
    </div>
  );
}
