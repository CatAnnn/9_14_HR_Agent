import { ArrowRight } from 'lucide-react';
import { useLanguage } from '../i18n/LanguageContext';

interface LandingAgentEntryProps {
  onStart: () => void | Promise<void>;
  onPrefetch?: () => void;
  starting: boolean;
}

export function LandingAgentEntry({ onStart, onPrefetch, starting }: LandingAgentEntryProps) {
  const { translate } = useLanguage();

  return (
    <section
      id="home-agent-entry"
      className="home-agent-entry"
      aria-labelledby="home-agent-entry-title"
    >
      <div className="home-section-inner home-agent-entry-inner">
        <h2 id="home-agent-entry-title" data-home-reveal="headline">Performance Feedback</h2>

        <div className="home-agent-entry-columns">
          <div className="home-agent-entry-copy" data-home-reveal="rise-delayed">
            <h3>{translate('更有准备地完成每一次关键人才对话')}</h3>
            <p>{translate('围绕员工背景与沟通目标，获得谈前指导、沉浸式多轮预演和四维复盘，将管理判断转化为清晰表达与后续行动。')}</p>
            <button
              type="button"
              onClick={onStart}
              onFocus={onPrefetch}
              onPointerEnter={onPrefetch}
              onPointerDown={onPrefetch}
              disabled={starting}
            >
              {starting ? (
                <span>{translate('正在进入')}</span>
              ) : (
                <>{translate('进入 Agent')} <ArrowRight aria-hidden="true" /></>
              )}
            </button>
          </div>

          <figure className="home-agent-entry-media" data-home-reveal="media">
            <img
              src="/assets/landing/solution-consulting.jpg"
              alt={translate('经理与员工围绕工作表现和发展目标展开沟通')}
              loading="lazy"
              decoding="async"
            />
          </figure>
        </div>
      </div>
    </section>
  );
}
