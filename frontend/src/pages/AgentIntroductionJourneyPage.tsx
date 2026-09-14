import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { AgentJourney } from '../components/AgentJourney';
import {
  agentIntroductionContent,
  localizeIntroductionText,
  type LocalizedIntroductionText,
} from '../content/agent-introduction-content';
import { useWorkflow } from '../context/WorkflowContext';
import { useLanguage } from '../i18n/LanguageContext';
import '../styles/agent-introduction-journey.css';

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

export default function AgentIntroductionJourneyPage() {
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
    <div className="agent-introduction-journey-page">
      <main className="agent-introduction-journey-main">
        <article aria-labelledby="agent-introduction-journey-title">
          <header className="agent-introduction-journey-hero">
            <div className="agent-introduction-journey-heading">
              <h1 id="agent-introduction-journey-title">{agentIntroductionContent.title}</h1>
            </div>
            <div className="agent-introduction-journey-intro-copy">
              <p className="agent-introduction-journey-subtitle">
                {localize(agentIntroductionContent.subtitle)}
              </p>
              <section
                className="agent-introduction-journey-purpose"
                aria-labelledby="agent-introduction-journey-purpose-title"
              >
                <h2 id="agent-introduction-journey-purpose-title">
                  {localize(agentIntroductionContent.purposeTitle)}
                </h2>
                <div>
                  {agentIntroductionContent.purpose.map((paragraph) => (
                    <p key={paragraph.zh}>{localize(paragraph)}</p>
                  ))}
                </div>
              </section>
            </div>
          </header>

          <AgentJourney
            content={agentIntroductionContent}
            localize={localize}
            starting={starting}
            onStart={() => void start()}
            onPrefetch={requestWorkspacePreload}
          />
        </article>
      </main>
    </div>
  );
}
