import { useLanguage } from '../i18n/LanguageContext';
import type { KnowledgeCitation } from '../types/domain';
import { normalizeDisplayText } from '../utils/displayText';
import {
  collectKnowledgeCitationSources,
  KNOWLEDGE_SCOPE_ENGLISH_LABELS,
} from '../utils/knowledgeCitations';

const MAX_PRINT_QUOTE_CHARACTERS = 680;
function printQuote(value: string) {
  const quote = normalizeDisplayText(value).trim();
  return quote.length > MAX_PRINT_QUOTE_CHARACTERS
    ? `${quote.slice(0, MAX_PRINT_QUOTE_CHARACTERS - 1).trimEnd()}…`
    : quote;
}

export default function KnowledgeCitationPrintAppendix({
  citations = [],
}: {
  citations?: KnowledgeCitation[];
}) {
  const { translate } = useLanguage();
  const sources = collectKnowledgeCitationSources(citations);
  if (!sources.length) return null;

  return (
    <section className="knowledge-citation-print-appendix">
      <h2>{translate('知识引用', 'Knowledge references')}</h2>
      <p>{translate(
        '以下内容为报告中知识高亮对应的直接依据或相关原文。',
        'The following source excerpts support the knowledge highlights in this report.',
      )}</p>
      <ol>
        {sources.map((source) => (
          <li key={source.id}>
            <strong>{normalizeDisplayText(source.title)}</strong>
            <span>
              {translate(
                source.sourceTypeLabel,
                source.sourceTypeLabel === '知识库' ? 'Knowledge base' : source.sourceTypeLabel,
              )}
              {' · '}
              {translate(
                source.scopeLabel,
                KNOWLEDGE_SCOPE_ENGLISH_LABELS[source.scopeLabel] || source.scopeLabel,
              )}
            </span>
            <ul>
              {source.quotes.map((quote) => (
                <li key={quote}><mark>{printQuote(quote)}</mark></li>
              ))}
            </ul>
          </li>
        ))}
      </ol>
    </section>
  );
}
