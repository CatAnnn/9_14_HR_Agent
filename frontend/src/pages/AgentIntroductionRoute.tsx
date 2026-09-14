import AgentIntroductionJourneyPage from './AgentIntroductionJourneyPage';
import AgentIntroductionPage from './AgentIntroductionPage';
import { getAgentIntroductionVariant } from '../utils/runtimeConfig';

export default function AgentIntroductionRoute() {
  return getAgentIntroductionVariant() === 'journey'
    ? <AgentIntroductionJourneyPage />
    : <AgentIntroductionPage />;
}
