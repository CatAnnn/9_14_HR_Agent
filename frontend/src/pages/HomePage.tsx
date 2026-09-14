import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  LandingAbout,
  LandingFooter,
  LandingHeader,
  LandingHero,
  LandingResourceCenter,
  LandingSolutions,
} from '../components/LandingExperience';
import { LandingAgentEntry } from '../components/LandingAgentEntry';
import { homeExperienceContent } from '../content/home-experience-content';
import { useViewportReveal } from '../hooks/useViewportReveal';
import { scheduleIdlePrefetch } from '../utils/prefetch';
import '../styles/home-page.css';

const loadLandingChatbot = () => import('../components/LandingChatbot');
const LazyLandingChatbot = lazy(() => loadLandingChatbot()
  .then(({ LandingChatbot }) => ({ default: LandingChatbot })));

const loadAgentIntroductionEntry = () => import('./AgentIntroductionRoute');

export default function HomePage() {
  const navigate = useNavigate();
  const [starting, setStarting] = useState(false);
  const [chatbotReady, setChatbotReady] = useState(false);
  const pageRef = useRef<HTMLDivElement>(null);
  useViewportReveal(pageRef);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => setChatbotReady(true));
    return () => window.cancelAnimationFrame(frame);
  }, []);

  useEffect(() => scheduleIdlePrefetch(loadAgentIntroductionEntry, {
    delayMs: 2400,
    idleTimeoutMs: 2400,
  }), []);

  const start = () => {
    if (starting) return;
    setStarting(true);
    navigate('/app/introduction');
  };

  return (
    <div ref={pageRef} className="home-page">
      <LandingHeader navigation={homeExperienceContent.navigation} inverse />
      <main id="home-main" className="home-main">
        <LandingHero hero={homeExperienceContent.hero} />
        <LandingAgentEntry
          onStart={start}
          onPrefetch={() => void loadAgentIntroductionEntry()}
          starting={starting}
        />
        <LandingResourceCenter resources={homeExperienceContent.resourceModules} />
        <LandingSolutions solutions={homeExperienceContent.solutions} />
        <LandingAbout content={homeExperienceContent.about} />
      </main>
      <LandingFooter />
      {chatbotReady && (
        <Suspense fallback={null}>
          <LazyLandingChatbot />
        </Suspense>
      )}
    </div>
  );
}
