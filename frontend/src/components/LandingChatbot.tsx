import { useCallback, useEffect, useId, useRef, useState } from 'react';
import type { FormEvent, KeyboardEvent } from 'react';
import { MessageCircle, Send, X } from 'lucide-react';
import { api, type ResourceChatSource } from '../api/client';
import { useLanguage } from '../i18n/LanguageContext';
import { normalizeDisplayText, userFacingErrorMessage } from '../utils/displayText';
import '../styles/landing-chatbot.css';


type ChatRole = 'user' | 'assistant';

interface ChatMessage {
  id: string;
  role: ChatRole;
  content: string;
  sources?: ResourceChatSource[];
}

const WELCOME_MESSAGE: ChatMessage = {
  id: 'welcome',
  role: 'assistant',
  content: '你好，我可以根据现有学习资源回答战略分析、教练辅导、反馈评估和人才发展相关问题。',
};

function messageId() {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
}

export function LandingChatbot() {
  const { language, translate } = useLanguage();
  const titleId = useId();
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([WELCOME_MESSAGE]);
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const [error, setError] = useState('');
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const messageEndRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const activeRequestRef = useRef<AbortController | null>(null);
  const requestSequenceRef = useRef(0);
  const languageRef = useRef(language);
  languageRef.current = language;

  const closeChat = useCallback(() => {
    setOpen(false);
    window.requestAnimationFrame(() => triggerRef.current?.focus());
  }, []);

  useEffect(() => {
    requestSequenceRef.current += 1;
    activeRequestRef.current?.abort();
    activeRequestRef.current = null;
    setMessages([WELCOME_MESSAGE]);
    setError('');
    setSending(false);
  }, [language]);

  useEffect(() => () => {
    requestSequenceRef.current += 1;
    activeRequestRef.current?.abort();
  }, []);

  useEffect(() => {
    if (!open) return undefined;
    const frame = window.requestAnimationFrame(() => inputRef.current?.focus());
    const closeOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') closeChat();
    };
    window.addEventListener('keydown', closeOnEscape);
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener('keydown', closeOnEscape);
    };
  }, [closeChat, open]);

  useEffect(() => {
    const input = inputRef.current;
    if (!input) return;
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, 92)}px`;
  }, [draft, open]);

  useEffect(() => {
    if (!open) return;
    messageEndRef.current?.scrollIntoView({
      block: 'end',
      behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
    });
  }, [messages, open, sending]);

  const sendMessage = async () => {
    const message = draft.trim();
    if (!message || sending) return;

    const previousMessages = messages;
    const userMessage: ChatMessage = {
      id: messageId(),
      role: 'user',
      content: message,
    };
    setMessages((current) => [...current, userMessage]);
    setDraft('');
    setError('');
    setSending(true);
    const requestLanguage = language;
    const requestSequence = ++requestSequenceRef.current;
    const controller = new AbortController();
    activeRequestRef.current?.abort();
    activeRequestRef.current = controller;
    const isCurrentRequest = () => (
      requestSequence === requestSequenceRef.current
      && requestLanguage === languageRef.current
      && !controller.signal.aborted
    );
    try {
      const response = await api.sendResourceChatMessage({
        message,
        history: previousMessages
          .filter((item) => item.id !== WELCOME_MESSAGE.id)
          .slice(-8)
          .map(({ role, content }) => ({ role, content })),
      }, controller.signal);
      if (!isCurrentRequest()) return;
      setMessages((current) => [
        ...current,
        {
          id: messageId(),
          role: 'assistant',
          content: normalizeDisplayText(response.answer),
          sources: response.sources,
        },
      ]);
    } catch (requestError) {
      if (!isCurrentRequest()) return;
      setError(userFacingErrorMessage(
        requestError,
        undefined,
        translate('暂时无法回答，请稍后重试。'),
      ));
    } finally {
      if (requestSequence === requestSequenceRef.current) {
        activeRequestRef.current = null;
        setSending(false);
      }
    }
  };

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    void sendMessage();
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      void sendMessage();
    }
  };

  return (
    <div className={`landing-chatbot${open ? ' is-open' : ''}`}>
      <section
        className="landing-chatbot-panel"
        role="dialog"
        aria-modal="false"
        aria-labelledby={titleId}
        aria-hidden={!open}
        inert={!open}
      >
          <header className="landing-chatbot-header">
            <span className="landing-chatbot-mark" aria-hidden="true">
              <MessageCircle />
            </span>
            <span className="landing-chatbot-heading">
              <strong id={titleId}>{translate('HR 资源助手')}</strong>
              <span><i aria-hidden="true" />{translate('在线')}</span>
            </span>
            <button
              type="button"
              className="landing-chatbot-close"
              onClick={closeChat}
              aria-label={translate('关闭资源助手')}
              title={translate('关闭')}
            >
              <X />
            </button>
          </header>

          <div className="landing-chatbot-messages" aria-live={open ? 'polite' : 'off'}>
            {messages.map((item) => (
              <article
                className={`landing-chatbot-message is-${item.role}`}
                key={item.id}
              >
                <div>{item.id === WELCOME_MESSAGE.id ? translate(item.content) : item.content}</div>
                {!!item.sources?.length && (
                  <p className="landing-chatbot-sources">
                    {translate('参考：')}{item.sources.map((source) => source.title).join('、')}
                  </p>
                )}
              </article>
            ))}
            {sending && (
              <div className="landing-chatbot-thinking" aria-label={translate('正在生成回答')}>
                <i /><i /><i />
              </div>
            )}
            {error && <p className="landing-chatbot-error" role="alert">{error}</p>}
            <div ref={messageEndRef} />
          </div>

          <form className="landing-chatbot-composer" onSubmit={handleSubmit}>
            <textarea
              ref={inputRef}
              value={draft}
              onChange={(event) => setDraft(event.target.value.slice(0, 2_000))}
              onKeyDown={handleKeyDown}
              rows={1}
              placeholder={translate('输入你的问题')}
              aria-label={translate('输入资源问题')}
            />
            <button
              type="submit"
              disabled={!draft.trim() || sending}
              aria-label={translate('发送消息')}
              title={translate('发送')}
            >
              <Send />
            </button>
          </form>
      </section>

      <button
        ref={triggerRef}
        type="button"
        className="landing-chatbot-trigger"
        onClick={() => {
          if (open) closeChat();
          else setOpen(true);
        }}
        aria-expanded={open}
        aria-label={translate(open ? '关闭资源助手' : '打开资源助手')}
        title={translate(open ? '关闭资源助手' : '打开资源助手')}
      >
        {open ? <X /> : <MessageCircle />}
      </button>
    </div>
  );
}
