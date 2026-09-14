import { useEffect, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
  agentIntroductionContent,
  localizeIntroductionText,
  type LocalizedIntroductionText,
} from '../content/agent-introduction-content';
import { useWorkflow } from '../context/WorkflowContext';
import { useLanguage } from '../i18n/LanguageContext';
import '../styles/agent-introduction.css';

let workspacePreload: Promise<void> | null = null;

const preloadWorkspaceEntry = (): Promise<void> => {
  if (!workspacePreload) {
    workspacePreload = Promise.all([
      import('./WorkspacePage'),
      import('./steps/ProfileStep'),
    ]).then(() => undefined).catch((error: unknown) => {
      workspacePreload = null;
      throw error;
    });
  }
  return workspacePreload;
};

const requestWorkspacePreload = (): void => {
  void preloadWorkspaceEntry().catch(() => undefined);
};

export default function AgentIntroductionPage() {
  const navigate = useNavigate();
  const { startNewSession } = useWorkflow();
  const { language } = useLanguage();
  const [starting, setStarting] = useState(false);
  const startingRef = useRef(false);

  const localize = (value: LocalizedIntroductionText) => (
    localizeIntroductionText(value, language)
  );

  useEffect(() => {
    const timer = window.setTimeout(requestWorkspacePreload, 900);
    return () => window.clearTimeout(timer);
  }, []);

  const start = async () => {
    if (startingRef.current) return;
    startingRef.current = true;
    setStarting(true);
    try {
      await startNewSession();
      navigate('/app/profile');
    } catch {
      // runTask exposes the normalized failure through the global toast.
    } finally {
      startingRef.current = false;
      setStarting(false);
    }
  };

  return (
    <div className="agent-introduction-page">
      <main className="agent-introduction-main">
        <article className="agent-introduction-document" aria-labelledby="agent-introduction-title">
          <header className="agent-introduction-heading">
            <h1 id="agent-introduction-title">{agentIntroductionContent.title}</h1>
            <p>{localize(agentIntroductionContent.subtitle)}</p>
          </header>

          <section className="agent-introduction-section" aria-labelledby="agent-introduction-purpose-title">
            <h2 id="agent-introduction-purpose-title">
              {localize(agentIntroductionContent.purposeTitle)}
            </h2>
            {agentIntroductionContent.purpose.map((paragraph) => (
              <p key={paragraph.zh}>{localize(paragraph)}</p>
            ))}
          </section>

          <section className="agent-introduction-section" aria-labelledby="agent-introduction-steps-title">
            <h2 id="agent-introduction-steps-title">
              {localize(agentIntroductionContent.stepsTitle)}
            </h2>
            <ol className="agent-introduction-step-list">
              {agentIntroductionContent.steps.map((step) => (
                <li key={step.id}>
                  <h3>{localize(step.title)}</h3>
                  <div className="agent-introduction-step-copy">
                    <p>{localize(step.action[0])}</p>
                    {step.options && (
                      <ul>
                        {step.options.map((option) => (
                          <li key={option.zh}>{localize(option)}</li>
                        ))}
                      </ul>
                    )}
                    {step.action.slice(1).map((paragraph, paragraphIndex) => (
                      <p key={`${step.id}-action-${paragraphIndex + 1}`}>{localize(paragraph)}</p>
                    ))}
                    {step.note && <p>{localize(step.note)}</p>}
                    <p>{localize(step.meaning)}</p>
                  </div>
                </li>
              ))}
            </ol>
          </section>

          <footer className="agent-introduction-actions">
            <Link to="/">{localize(agentIntroductionContent.backLabel)}</Link>
            <button
              type="button"
              onClick={start}
              onFocus={requestWorkspacePreload}
              onPointerEnter={requestWorkspacePreload}
              disabled={starting}
            >
              {localize(starting
                ? agentIntroductionContent.startingLabel
                : agentIntroductionContent.startLabel)}
            </button>
          </footer>
        </article>
      </main>
    </div>
  );
}
