import {
  Fragment,
  memo,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { createPortal } from 'react-dom';

import { useLanguage } from '../i18n/LanguageContext';
import type {
  KnowledgeCitation,
  KnowledgeCitationAnchor,
} from '../types/domain';
import { normalizeDisplayText } from '../utils/displayText';
import { createExclusiveSurfaceCoordinator } from '../utils/exclusiveSurface';
import {
  KNOWLEDGE_SCOPE_ENGLISH_LABELS,
  KNOWLEDGE_SCOPE_LABELS,
  knowledgeCitationIdentity as citationIdentity,
  matchingKnowledgeCitations,
  normalizeKnowledgeScope as normalizedScope,
} from '../utils/knowledgeCitations';
import {
  compileKnowledgeMarkdown,
  withKnowledgeBlockText,
  type KnowledgeInlineToken,
  type KnowledgeQuoteBlock,
} from '../utils/knowledgeMarkdown';
import '../styles/knowledge-markdown.css';

interface KnowledgeLinkedTextProps {
  text: string;
  target: string;
  citations?: KnowledgeCitation[];
  className?: string;
  showRelatedSources?: boolean;
}

interface TooltipPosition {
  left: number;
  top: number;
  width: number;
  maxHeight: number;
  placement: 'top' | 'bottom';
}

interface ComparableText {
  value: string;
  starts: number[];
  ends: number[];
}

interface KnowledgeReference {
  citation: KnowledgeCitation;
  anchor?: KnowledgeCitationAnchor;
}

interface KnowledgeMark extends KnowledgeReference {
  start: number;
  end: number;
}

interface KnowledgeSegment {
  text: string;
  references: KnowledgeReference[];
}

interface KnowledgePresentation {
  segments: KnowledgeSegment[];
  relatedReferences: KnowledgeReference[];
}


type KnowledgeSourceType = 'knowledge_base' | 'skill';

const VIEWPORT_MARGIN = 16;
const TOOLTIP_GAP = 8;
const TOOLTIP_MAX_WIDTH = 600;
const MAX_MATCHING_QUOTE_CHARACTERS = 16000;
const MIN_CONTEXT_MATCH_CHARACTERS = 12;
const MAX_HIGHLIGHT_CHARACTERS = 160;
const MAX_HIGHLIGHT_LINES = 2;
const MAX_HIGHLIGHT_SENTENCE_BREAKS = 2;
const MAX_HIGHLIGHT_LIST_SEPARATORS = 3;
const MAX_RELEVANT_EXCERPT_CHARACTERS = 520;
const MAX_RELEVANT_CONTEXT_CHARACTERS = 960;
const MAX_RELEVANT_HEADING_CHARACTERS = 160;
const MAX_VISIBLE_SOURCES = 4;

// KnowledgeLinkedText is used by both Guidance and Coach reports. Keeping the
// coordinator at module scope guarantees that their portalled tooltips share a
// single owner, even when the triggers live in different component subtrees.
const knowledgeTooltipCoordinator = createExclusiveSurfaceCoordinator();

const EXACT_SOURCE_SCOPES = new Set([
  'career',
  'culture',
  'job_level',
  'performance',
  'redline',
]);

function citationSourceType(citation: KnowledgeCitation): KnowledgeSourceType {
  return citation.source_type === 'skill' ? 'skill' : 'knowledge_base';
}

function referenceIdentity(reference: KnowledgeReference) {
  const anchor = reference.anchor;
  return [
    citationIdentity(reference.citation),
    anchor?.target || '',
    anchor?.highlight_text || '',
    anchor?.source_quote || '',
    anchor?.source_context || '',
  ].join(':');
}

function referencePresentationScore(reference: KnowledgeReference) {
  const sourceContext = String(reference.anchor?.source_context || '').trim();
  const sourceQuote = String(reference.anchor?.source_quote || '').trim();
  return (
    Number(Boolean(sourceContext)) * 8
    + Number(Boolean(sourceQuote)) * 4
    + Number(citationSourceType(reference.citation) === 'knowledge_base') * 2
    + Number(Boolean(reference.anchor))
  );
}

function knowledgeReferenceTokens(input: string) {
  const tokens = String(input || '')
    .normalize('NFKC')
    .match(/(?:SL\d+(?:G\d+)?|ETP\d+|TP\d+|G\d+|P\d+)/giu) || [];
  return Array.from(new Set(tokens.map((token) => token.toLowerCase())));
}

function isPreciseHighlight(value: string) {
  const normalized = normalizeDisplayText(value).trim();
  if (!normalized || normalized.length > MAX_HIGHLIGHT_CHARACTERS) return false;
  const nonEmptyLines = value.split(/\r?\n/u).filter((line) => line.trim());
  if (nonEmptyLines.length > MAX_HIGHLIGHT_LINES) {
    return false;
  }
  const sentenceBreaks = normalized.match(/[。！？.!?；;]/g) || [];
  if (sentenceBreaks.length > MAX_HIGHLIGHT_SENTENCE_BREAKS) return false;
  return (
    (normalized.match(/[,，、]/g) || []).length
    <= MAX_HIGHLIGHT_LIST_SEPARATORS
  );
}

function comparableText(input: string): ComparableText {
  const characters: string[] = [];
  const starts: number[] = [];
  const ends: number[] = [];
  let offset = 0;

  for (const sourceCharacter of input) {
    const start = offset;
    offset += sourceCharacter.length;
    const normalized = sourceCharacter.normalize('NFKC').toLowerCase();
    for (const character of normalized) {
      if (!/[\p{L}\p{N}+#]/u.test(character)) continue;
      characters.push(character);
      for (let codeUnit = 0; codeUnit < character.length; codeUnit += 1) {
        starts.push(start);
        ends.push(offset);
      }
    }
  }

  return {
    value: characters.join(''),
    starts,
    ends,
  };
}

function originalRange(
  comparable: ComparableText,
  normalizedStart: number,
  normalizedEnd: number,
) {
  if (
    normalizedStart < 0
    || normalizedEnd <= normalizedStart
    || normalizedEnd > comparable.starts.length
  ) {
    return null;
  }
  return {
    start: comparable.starts[normalizedStart],
    end: comparable.ends[normalizedEnd - 1],
  };
}

function longestCommonRange(
  text: ComparableText,
  quote: ComparableText,
  segmentStart = 0,
  segmentEnd = text.value.length,
) {
  const quoteValue = quote.value.slice(0, MAX_MATCHING_QUOTE_CHARACTERS);
  if (
    !quoteValue
    || segmentEnd - segmentStart < MIN_CONTEXT_MATCH_CHARACTERS
    || quoteValue.length < MIN_CONTEXT_MATCH_CHARACTERS
  ) {
    return null;
  }

  // A suffix automaton finds the exact longest shared phrase in linear time,
  // including highly repetitive source material.
  const states: Array<{
    length: number;
    link: number;
    transitions: Map<string, number>;
  }> = [{ length: 0, link: -1, transitions: new Map() }];
  let lastState = 0;

  for (let quoteIndex = 0; quoteIndex < quoteValue.length; quoteIndex += 1) {
    const character = quoteValue[quoteIndex];
    const currentState = states.length;
    states.push({
      length: states[lastState].length + 1,
      link: 0,
      transitions: new Map(),
    });

    let sourceState = lastState;
    while (
      sourceState >= 0
      && !states[sourceState].transitions.has(character)
    ) {
      states[sourceState].transitions.set(character, currentState);
      sourceState = states[sourceState].link;
    }

    if (sourceState >= 0) {
      const destinationState = states[sourceState].transitions.get(character)!;
      if (states[sourceState].length + 1 === states[destinationState].length) {
        states[currentState].link = destinationState;
      } else {
        const cloneState = states.length;
        states.push({
          length: states[sourceState].length + 1,
          link: states[destinationState].link,
          transitions: new Map(states[destinationState].transitions),
        });
        while (
          sourceState >= 0
          && states[sourceState].transitions.get(character) === destinationState
        ) {
          states[sourceState].transitions.set(character, cloneState);
          sourceState = states[sourceState].link;
        }
        states[destinationState].link = cloneState;
        states[currentState].link = cloneState;
      }
    }
    lastState = currentState;
  }

  let currentState = 0;
  let currentLength = 0;
  let bestLength = 0;
  let bestTextStart = 0;
  for (let textIndex = segmentStart; textIndex < segmentEnd; textIndex += 1) {
    const character = text.value[textIndex];
    while (
      currentState !== 0
      && !states[currentState].transitions.has(character)
    ) {
      currentState = states[currentState].link;
      currentLength = Math.min(currentLength, states[currentState].length);
    }

    const nextState = states[currentState].transitions.get(character);
    if (nextState === undefined) {
      currentState = 0;
      currentLength = 0;
      continue;
    }
    currentState = nextState;
    currentLength += 1;
    if (currentLength > bestLength) {
      bestLength = currentLength;
      bestTextStart = textIndex - currentLength + 1;
    }
  }

  if (bestLength < MIN_CONTEXT_MATCH_CHARACTERS) return null;
  return {
    start: bestTextStart,
    end: bestTextStart + bestLength,
    length: bestLength,
  };
}

function exactAnchorMarks(
  displayText: string,
  citation: KnowledgeCitation,
  target: string,
): KnowledgeMark[] {
  const marks: KnowledgeMark[] = [];
  for (const anchor of citation.anchors || []) {
    if (anchor.target !== target) continue;
    const highlightText = normalizeDisplayText(String(anchor.highlight_text || ''));
    if (!isPreciseHighlight(highlightText)) continue;

    const start = displayText.indexOf(highlightText);
    if (start < 0) continue;
    marks.push({
      start,
      end: start + highlightText.length,
      citation,
      anchor,
    });
  }
  return marks;
}

function compareKnowledgeMarks(left: KnowledgeMark, right: KnowledgeMark) {
  const exactDifference = Number(!left.anchor) - Number(!right.anchor);
  if (exactDifference) return exactDifference;

  const sourceScopeDifference = Number(
    !EXACT_SOURCE_SCOPES.has(normalizedScope(left.citation.scope)),
  ) - Number(!EXACT_SOURCE_SCOPES.has(normalizedScope(right.citation.scope)));
  if (sourceScopeDifference) return sourceScopeDifference;

  const lengthDifference = (left.end - left.start) - (right.end - right.start);
  if (lengthDifference) return lengthDifference;

  const sourceDifference = Number(citationSourceType(left.citation) === 'skill')
    - Number(citationSourceType(right.citation) === 'skill');
  if (sourceDifference) return sourceDifference;

  const quoteDifference = String(left.citation.quote || '').length
    - String(right.citation.quote || '').length;
  if (quoteDifference) return quoteDifference;
  return citationIdentity(left.citation).localeCompare(citationIdentity(right.citation));
}

function selectKnowledgeMarks(marks: KnowledgeMark[]) {
  if (marks.length < 2) return marks;

  const ordered = [...marks].sort((left, right) => (
    left.start - right.start
    || right.end - left.end
    || compareKnowledgeMarks(left, right)
  ));
  const selected: KnowledgeMark[] = [];
  let cluster: KnowledgeMark[] = [];
  let clusterEnd = -1;

  const commitCluster = () => {
    if (!cluster.length) return;
    const winner = [...cluster].sort(compareKnowledgeMarks)[0];
    const seen = new Set<string>();
    for (const mark of cluster) {
      if (mark.start !== winner.start || mark.end !== winner.end) continue;
      const identity = referenceIdentity(mark);
      if (seen.has(identity)) continue;
      seen.add(identity);
      selected.push(mark);
    }
  };

  for (const mark of ordered) {
    if (!cluster.length || mark.start < clusterEnd) {
      cluster.push(mark);
      clusterEnd = Math.max(clusterEnd, mark.end);
      continue;
    }
    commitCluster();
    cluster = [mark];
    clusterEnd = mark.end;
  }
  commitCluster();
  return selected.sort((left, right) => left.start - right.start || left.end - right.end);
}

function mergeKnowledgeSegments(
  displayText: string,
  marks: KnowledgeMark[],
): KnowledgeSegment[] {
  if (!marks.length) return [{ text: displayText, references: [] }];

  const boundaries = Array.from(new Set([
    0,
    displayText.length,
    ...marks.flatMap((mark) => [mark.start, mark.end]),
  ]))
    .filter((boundary) => boundary >= 0 && boundary <= displayText.length)
    .sort((left, right) => left - right);

  const segments: KnowledgeSegment[] = [];
  for (let index = 0; index < boundaries.length - 1; index += 1) {
    const start = boundaries[index];
    const end = boundaries[index + 1];
    const segmentText = displayText.slice(start, end);
    if (!segmentText) continue;

    const seen = new Set<string>();
    const segmentReferences = marks.flatMap((mark) => {
      if (mark.start >= end || mark.end <= start) return [];
      const reference = {
        citation: mark.citation,
        anchor: mark.anchor,
      };
      const identity = referenceIdentity(reference);
      if (seen.has(identity)) return [];
      seen.add(identity);
      return [reference];
    });

    const previous = segments[segments.length - 1];
    const previousIds = previous?.references.map(referenceIdentity).join('|') || '';
    const currentIds = segmentReferences.map(referenceIdentity).join('|');
    if (previous && previousIds === currentIds) {
      previous.text += segmentText;
    } else {
      segments.push({ text: segmentText, references: segmentReferences });
    }
  }
  return segments;
}

function targetReferences(
  citation: KnowledgeCitation,
  target: string,
): KnowledgeReference[] {
  const anchors = (citation.anchors || []).filter((anchor) => anchor.target === target);
  return anchors.length
    ? anchors.map((anchor) => ({ citation, anchor }))
    : [{ citation }];
}

function buildKnowledgePresentation(
  displayText: string,
  citations: KnowledgeCitation[],
  target: string,
): KnowledgePresentation {
  const targetCitations = matchingKnowledgeCitations(citations, target);
  // Only backend-validated anchors may paint a phrase in the generated text.
  // Legacy target + quote citations remain visible as related field sources,
  // avoiding a fuzzy match that could visually claim evidence for the wrong phrase.
  const marks = targetCitations.flatMap((citation) => (
    exactAnchorMarks(displayText, citation, target)
  ));
  const segments = mergeKnowledgeSegments(displayText, selectKnowledgeMarks(marks));
  const linkedReferences = new Set(
    segments.flatMap((segment) => segment.references.map(referenceIdentity)),
  );
  const relatedReferences = targetCitations
    .flatMap((citation) => targetReferences(citation, target))
    .filter((reference) => !linkedReferences.has(referenceIdentity(reference)));
  return { segments, relatedReferences };
}

function renderKnowledgeInline(tokens: KnowledgeInlineToken[]) {
  return tokens.map((token, index) => {
    const key = token.type + ':' + String(index) + ':' + token.text;
    if (token.type === 'strong') return <strong key={key}>{token.text}</strong>;
    if (token.type === 'emphasis') return <em key={key}>{token.text}</em>;
    if (token.type === 'code') return <code key={key}>{token.text}</code>;
    if (token.type === 'strike') return <s key={key}>{token.text}</s>;
    if (token.type === 'link' && token.href) {
      return (
        <a href={token.href} key={key} rel="noopener noreferrer" target="_blank">
          {token.text}
        </a>
      );
    }
    return <Fragment key={key}>{token.text}</Fragment>;
  });
}

function comparableMatchLength(leftValue: string, rightValue: string) {
  const left = comparableText(leftValue);
  const right = comparableText(rightValue);
  if (!left.value || !right.value) return 0;
  if (left.value.includes(right.value)) return right.value.length;
  if (right.value.includes(left.value)) return left.value.length;
  return longestCommonRange(left, right)?.length || 0;
}


function referenceTokenMatch(blockText: string, highlightedText: string) {
  const highlightedTokens = knowledgeReferenceTokens(highlightedText);
  if (!highlightedTokens.length) return { primary: 0, coverage: 0, unrelated: 0 };

  const blockTokens = knowledgeReferenceTokens(blockText);
  const highlightedSet = new Set(highlightedTokens);
  return {
    primary: Number(Boolean(blockTokens[0] && highlightedSet.has(blockTokens[0]))),
    coverage: highlightedTokens.filter((token) => blockTokens.includes(token)).length,
    unrelated: blockTokens.filter((token) => !highlightedSet.has(token)).length,
  };
}

function matchingOriginalRange(sourceText: string, highlightedText: string) {
  const source = comparableText(sourceText);
  const highlighted = comparableText(highlightedText);
  if (!source.value || !highlighted.value) return null;

  const directStart = source.value.indexOf(highlighted.value);
  if (directStart >= 0) {
    return originalRange(source, directStart, directStart + highlighted.value.length);
  }

  const match = longestCommonRange(source, highlighted);
  return match ? originalRange(source, match.start, match.end) : null;
}

function limitRelevantBlock(
  block: KnowledgeQuoteBlock,
  highlightedText: string,
  maxCharacters = MAX_RELEVANT_EXCERPT_CHARACTERS,
): KnowledgeQuoteBlock {
  if (block.text.length <= maxCharacters) return block;

  const range = matchingOriginalRange(block.text, highlightedText);
  if (!range) {
    return withKnowledgeBlockText(
      block,
      block.text.slice(0, Math.max(1, maxCharacters - 1)).trimEnd() + '…',
    );
  }

  const matchLength = range.end - range.start;
  const contextBudget = Math.max(0, maxCharacters - matchLength);
  let start = Math.max(0, range.start - Math.floor(contextBudget * 0.45));
  let end = Math.min(block.text.length, start + maxCharacters);
  start = Math.max(0, end - maxCharacters);
  const excerpt = block.text.slice(start, end).trim();
  return withKnowledgeBlockText(
    block,
    (start > 0 ? '…' : '') + excerpt + (end < block.text.length ? '…' : ''),
  );
}

function hasConflictingReferenceTokens(
  blockText: string,
  highlightedText: string,
) {
  const highlightedTokens = knowledgeReferenceTokens(highlightedText);
  const blockTokens = knowledgeReferenceTokens(blockText);
  if (!highlightedTokens.length || !blockTokens.length) return false;

  const highlightedSet = new Set(highlightedTokens);
  return blockTokens.some((token) => !highlightedSet.has(token));
}

function selectRelevantSentenceWindow(
  block: KnowledgeQuoteBlock,
  highlightedText: string,
): KnowledgeQuoteBlock {
  const sentences = (block.text.match(/[^。！？.!?；;\n]+[。！？.!?；;]?/g) || [])
    .map((sentence) => sentence.trim())
    .filter(Boolean);
  if (sentences.length < 2) return limitRelevantBlock(block, highlightedText);

  let selectedIndex = 0;
  let selectedPrimaryReference = -1;
  let selectedReferenceCoverage = -1;
  let selectedScore = -1;
  let selectedUnrelatedReferences = Number.POSITIVE_INFINITY;
  sentences.forEach((candidate, index) => {
    const referenceMatch = referenceTokenMatch(candidate, highlightedText);
    const score = comparableMatchLength(candidate, highlightedText);
    if (
      referenceMatch.primary > selectedPrimaryReference
      || (
        referenceMatch.primary === selectedPrimaryReference
        && referenceMatch.coverage > selectedReferenceCoverage
      )
      || (
        referenceMatch.primary === selectedPrimaryReference
        && referenceMatch.coverage === selectedReferenceCoverage
        && score > selectedScore
      )
      || (
        referenceMatch.primary === selectedPrimaryReference
        && referenceMatch.coverage === selectedReferenceCoverage
        && score === selectedScore
        && referenceMatch.unrelated < selectedUnrelatedReferences
      )
    ) {
      selectedIndex = index;
      selectedPrimaryReference = referenceMatch.primary;
      selectedReferenceCoverage = referenceMatch.coverage;
      selectedScore = score;
      selectedUnrelatedReferences = referenceMatch.unrelated;
    }
  });

  if (selectedReferenceCoverage <= 0 && selectedScore <= 0) {
    return limitRelevantBlock(block, highlightedText);
  }

  const selectedIndexes = [selectedIndex];
  let selectedCharacters = sentences[selectedIndex].length;
  for (const candidateIndex of [selectedIndex - 1, selectedIndex + 1]) {
    if (candidateIndex < 0 || candidateIndex >= sentences.length) continue;
    const candidate = sentences[candidateIndex];
    if (hasConflictingReferenceTokens(candidate, highlightedText)) continue;
    if (selectedCharacters + candidate.length + 1 > MAX_RELEVANT_EXCERPT_CHARACTERS) {
      continue;
    }
    selectedIndexes.push(candidateIndex);
    selectedCharacters += candidate.length + 1;
  }

  const selectedText = selectedIndexes
    .sort((left, right) => left - right)
    .map((index) => sentences[index])
    .join(' ');
  return limitRelevantBlock(
    withKnowledgeBlockText(block, selectedText),
    highlightedText,
  );
}

function knowledgeSectionBounds(
  blocks: KnowledgeQuoteBlock[],
  blockIndex: number,
) {
  let headingIndex = blocks[blockIndex]?.type === 'heading' ? blockIndex : -1;
  for (let index = blockIndex - 1; index >= 0; index -= 1) {
    if (blocks[index].type === 'heading') {
      headingIndex = index;
      break;
    }
  }

  let sectionEnd = blocks.length;
  for (let index = blockIndex + 1; index < blocks.length; index += 1) {
    if (blocks[index].type === 'heading') {
      sectionEnd = index;
      break;
    }
  }
  return {
    headingIndex,
    contentStart: headingIndex >= 0 ? headingIndex + 1 : 0,
    sectionEnd,
  };
}

function contentBlockForHeading(
  blocks: KnowledgeQuoteBlock[],
  headingIndex: number,
  sectionEnd: number,
  highlightedText: string,
) {
  let selectedIndex = -1;
  let selectedScore = -1;
  for (let index = headingIndex + 1; index < sectionEnd; index += 1) {
    const block = blocks[index];
    if (block.type === 'heading') break;
    if (hasConflictingReferenceTokens(block.text, highlightedText)) continue;
    const score = comparableMatchLength(block.text, highlightedText);
    if (selectedIndex < 0 || score > selectedScore) {
      selectedIndex = index;
      selectedScore = score;
    }
  }
  return selectedIndex;
}

function contextualKnowledgeBlocks(
  blocks: KnowledgeQuoteBlock[],
  blockIndex: number,
  highlightedText: string,
) {
  let selectedIndex = blockIndex;
  let bounds = knowledgeSectionBounds(blocks, selectedIndex);
  if (blocks[selectedIndex].type === 'heading') {
    const contentIndex = contentBlockForHeading(
      blocks,
      selectedIndex,
      bounds.sectionEnd,
      highlightedText,
    );
    if (contentIndex < 0) {
      return [
        limitRelevantBlock(
          blocks[selectedIndex],
          highlightedText,
          MAX_RELEVANT_HEADING_CHARACTERS,
        ),
      ];
    }
    selectedIndex = contentIndex;
    bounds = knowledgeSectionBounds(blocks, selectedIndex);
  }

  const selected = selectRelevantSentenceWindow(
    blocks[selectedIndex],
    highlightedText,
  );
  const heading = bounds.headingIndex >= 0
    && !hasConflictingReferenceTokens(
      blocks[bounds.headingIndex].text,
      highlightedText,
    )
    ? limitRelevantBlock(
      blocks[bounds.headingIndex],
      highlightedText,
      MAX_RELEVANT_HEADING_CHARACTERS,
    )
    : null;
  const neighborIndexes = [selectedIndex - 1, selectedIndex + 1].filter((index) => (
    index >= bounds.contentStart
    && index < bounds.sectionEnd
    && blocks[index].type !== 'heading'
    && !hasConflictingReferenceTokens(blocks[index].text, highlightedText)
  ));

  const selectedEntries = [
    ...(heading ? [{ index: bounds.headingIndex, block: heading }] : []),
    { index: selectedIndex, block: selected },
  ];
  let remainingCharacters = Math.max(
    0,
    MAX_RELEVANT_CONTEXT_CHARACTERS
      - selectedEntries.reduce((total, entry) => total + entry.block.text.length, 0),
  );
  neighborIndexes.forEach((index, neighborPosition) => {
    const neighborsRemaining = neighborIndexes.length - neighborPosition;
    const budget = Math.max(
      1,
      Math.floor(remainingCharacters / neighborsRemaining) - 2,
    );
    if (budget < 24) return;
    const block = limitRelevantBlock(blocks[index], highlightedText, budget);
    if (!block.text || block.text.length > remainingCharacters) return;
    selectedEntries.push({ index, block });
    remainingCharacters -= block.text.length;
  });

  const seen = new Set<string>();
  return selectedEntries
    .sort((left, right) => left.index - right.index)
    .flatMap(({ block }) => {
      const identity = block.type + ':' + block.text;
      if (seen.has(identity)) return [];
      seen.add(identity);
      return [block];
    });
}

function compileRelevantKnowledgeQuote(
  quote: string,
  highlightedText: string,
): KnowledgeQuoteBlock[] {
  const blocks = compileKnowledgeMarkdown(quote);
  if (blocks.length < 2) {
    return blocks.map((block) => selectRelevantSentenceWindow(block, highlightedText));
  }

  let bestIndex = 0;
  let bestPrimaryReference = -1;
  let bestReferenceCoverage = -1;
  let bestScore = -1;
  let bestUnrelatedReferences = Number.POSITIVE_INFINITY;
  let bestContentPriority = -1;
  blocks.forEach((block, index) => {
    const referenceMatch = referenceTokenMatch(block.text, highlightedText);
    const score = comparableMatchLength(block.text, highlightedText);
    const contentPriority = block.type === 'heading' ? 0 : 1;
    if (
      referenceMatch.primary > bestPrimaryReference
      || (
        referenceMatch.primary === bestPrimaryReference
        && referenceMatch.coverage > bestReferenceCoverage
      )
      || (
        referenceMatch.primary === bestPrimaryReference
        && referenceMatch.coverage === bestReferenceCoverage
        && score > bestScore
      )
      || (
        referenceMatch.primary === bestPrimaryReference
        && referenceMatch.coverage === bestReferenceCoverage
        && score === bestScore
        && referenceMatch.unrelated < bestUnrelatedReferences
      )
      || (
        referenceMatch.primary === bestPrimaryReference
        && referenceMatch.coverage === bestReferenceCoverage
        && score === bestScore
        && referenceMatch.unrelated === bestUnrelatedReferences
        && contentPriority > bestContentPriority
      )
    ) {
      bestIndex = index;
      bestPrimaryReference = referenceMatch.primary;
      bestReferenceCoverage = referenceMatch.coverage;
      bestScore = score;
      bestUnrelatedReferences = referenceMatch.unrelated;
      bestContentPriority = contentPriority;
    }
  });

  return contextualKnowledgeBlocks(blocks, bestIndex, highlightedText);
}

function KnowledgeReferenceTrigger({
  text,
  references,
  matchText = text,
  related = false,
}: {
  text: string;
  references: KnowledgeReference[];
  matchText?: string;
  related?: boolean;
}) {
  const { translate, translateTemplate } = useLanguage();
  const tooltipId = useId();
  const tooltipTitleId = `${tooltipId}-title`;
  const ownerRef = useRef<object>({});
  const triggerRef = useRef<HTMLButtonElement>(null);
  const tooltipRef = useRef<HTMLDivElement>(null);
  const closeTimerRef = useRef<number | null>(null);
  const positionFrameRef = useRef<number | null>(null);
  const [open, setOpen] = useState(false);
  const [pinned, setPinned] = useState(false);
  const [position, setPosition] = useState<TooltipPosition | null>(null);
  const preparedCitations = useMemo(() => {
    const seen = new Set<string>();
    return [...references]
      .sort((left, right) => (
        referencePresentationScore(right) - referencePresentationScore(left)
        || referenceIdentity(left).localeCompare(referenceIdentity(right))
      ))
      .flatMap((reference) => {
        const identity = [
          citationIdentity(reference.citation),
          reference.anchor?.source_quote || reference.citation.quote || '',
        ].join(':');
        if (seen.has(identity)) return [];
        seen.add(identity);
        const sourceContext = String(reference.anchor?.source_context || '').trim();
        const exactQuote = String(reference.anchor?.source_quote || '').trim();
        const sourceText = sourceContext
          || exactQuote
          || String(reference.citation.quote || '').trim();
        if (!sourceText) return [];
        return [{
          reference,
          sourceType: citationSourceType(reference.citation),
          isExact: Boolean(exactQuote),
          hasContext: Boolean(sourceContext),
          exactQuote,
          blocks: reference.anchor
            ? (
              sourceContext
              && normalizeDisplayText(sourceContext) !== normalizeDisplayText(exactQuote)
                ? compileKnowledgeMarkdown(sourceContext)
                : []
            )
            : compileRelevantKnowledgeQuote(
              sourceText,
              matchText,
            ),
        }];
      })
      .slice(0, MAX_VISIBLE_SOURCES);
  }, [matchText, references]);
  const tooltipTitle = related
    ? translate('相关来源', 'Related sources')
    : translate('引用依据', 'Source evidence');

  const cancelScheduledClose = useCallback(() => {
    if (closeTimerRef.current !== null) {
      window.clearTimeout(closeTimerRef.current);
      closeTimerRef.current = null;
    }
  }, []);

  const scheduleClose = useCallback(() => {
    cancelScheduledClose();
    if (pinned) return;
    closeTimerRef.current = window.setTimeout(() => {
      closeTimerRef.current = null;
      knowledgeTooltipCoordinator.release(ownerRef.current);
      setOpen(false);
    }, 180);
  }, [cancelScheduledClose, pinned]);

  const updatePosition = useCallback(() => {
    const trigger = triggerRef.current;
    if (!trigger) return;
    const triggerRect = trigger.getBoundingClientRect();
    const width = Math.min(
      TOOLTIP_MAX_WIDTH,
      Math.max(240, window.innerWidth - VIEWPORT_MARGIN * 2),
    );
    const left = Math.min(
      Math.max(triggerRect.left + triggerRect.width / 2 - width / 2, VIEWPORT_MARGIN),
      Math.max(VIEWPORT_MARGIN, window.innerWidth - width - VIEWPORT_MARGIN),
    );
    const availableBelow = Math.max(
      80,
      window.innerHeight - triggerRect.bottom - TOOLTIP_GAP - VIEWPORT_MARGIN,
    );
    const availableAbove = Math.max(
      80,
      triggerRect.top - TOOLTIP_GAP - VIEWPORT_MARGIN,
    );
    const contentHeight = tooltipRef.current?.scrollHeight || 320;
    const placement = availableBelow >= Math.min(contentHeight, 320)
      || availableBelow >= availableAbove
      ? 'bottom'
      : 'top';
    const maxHeight = placement === 'bottom' ? availableBelow : availableAbove;
    const renderedHeight = Math.min(contentHeight, maxHeight);
    const top = placement === 'bottom'
      ? triggerRect.bottom + TOOLTIP_GAP
      : Math.max(VIEWPORT_MARGIN, triggerRect.top - renderedHeight - TOOLTIP_GAP);
    setPosition((current) => {
      if (
        current
        && current.left === left
        && current.top === top
        && current.width === width
        && current.maxHeight === maxHeight
        && current.placement === placement
      ) {
        return current;
      }
      return { left, top, width, maxHeight, placement };
    });
  }, []);

  const cancelPositionUpdate = useCallback(() => {
    if (positionFrameRef.current !== null) {
      window.cancelAnimationFrame(positionFrameRef.current);
      positionFrameRef.current = null;
    }
  }, []);

  const schedulePositionUpdate = useCallback(() => {
    if (positionFrameRef.current !== null) return;
    positionFrameRef.current = window.requestAnimationFrame(() => {
      positionFrameRef.current = null;
      updatePosition();
    });
  }, [updatePosition]);

  const closeTooltip = useCallback(() => {
    cancelScheduledClose();
    cancelPositionUpdate();
    knowledgeTooltipCoordinator.release(ownerRef.current);
    setPinned(false);
    setOpen(false);
  }, [cancelPositionUpdate, cancelScheduledClose]);

  const showTooltip = useCallback(() => {
    cancelScheduledClose();
    knowledgeTooltipCoordinator.claim(ownerRef.current, closeTooltip);
    setOpen(true);
    updatePosition();
  }, [cancelScheduledClose, closeTooltip, updatePosition]);

  useLayoutEffect(() => {
    if (!open) return undefined;
    updatePosition();
    schedulePositionUpdate();
    return cancelPositionUpdate;
  }, [
    cancelPositionUpdate,
    open,
    preparedCitations,
    schedulePositionUpdate,
    updatePosition,
  ]);

  useEffect(() => {
    if (!open) return undefined;
    const reposition = () => schedulePositionUpdate();
    const resizeObserver = typeof ResizeObserver === 'undefined'
      ? null
      : new ResizeObserver(reposition);
    if (triggerRef.current) resizeObserver?.observe(triggerRef.current);
    window.addEventListener('resize', reposition);
    window.addEventListener('scroll', reposition, { capture: true, passive: true });
    return () => {
      resizeObserver?.disconnect();
      window.removeEventListener('resize', reposition);
      window.removeEventListener('scroll', reposition, true);
      cancelPositionUpdate();
    };
  }, [cancelPositionUpdate, open, schedulePositionUpdate]);

  useEffect(() => {
    if (!open || !pinned) return undefined;
    const closeOutside = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (triggerRef.current?.contains(target) || tooltipRef.current?.contains(target)) return;
      closeTooltip();
    };
    document.addEventListener('pointerdown', closeOutside);
    return () => document.removeEventListener('pointerdown', closeOutside);
  }, [closeTooltip, open, pinned]);

  useEffect(() => {
    if (!open) return undefined;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      closeTooltip();
      triggerRef.current?.focus();
    };
    document.addEventListener('keydown', closeOnEscape);
    return () => document.removeEventListener('keydown', closeOnEscape);
  }, [closeTooltip, open]);

  useEffect(() => () => {
    cancelScheduledClose();
    cancelPositionUpdate();
    knowledgeTooltipCoordinator.release(ownerRef.current);
  }, [cancelPositionUpdate, cancelScheduledClose]);

  return (
    <>
      <button
        ref={triggerRef}
        className={[
          'knowledge-reference-trigger',
          related ? 'is-related-source' : '',
        ].filter(Boolean).join(' ')}
        type="button"
        aria-label={related
          ? translateTemplate(
            '查看本字段的{tooltip_title}',
            'View related sources for this field',
            { tooltip_title: tooltipTitle },
          )
          : translateTemplate(
            '查看“{text}”采用的{tooltip_title}',
            'View source evidence for “{text}”',
            { text, tooltip_title: tooltipTitle },
          )}
        aria-controls={open ? tooltipId : undefined}
        aria-haspopup="dialog"
        aria-expanded={open}
        onMouseEnter={showTooltip}
        onMouseLeave={scheduleClose}
        onFocus={showTooltip}
        onBlur={scheduleClose}
        onKeyDown={(event) => {
          if (event.key !== 'Escape') return;
          closeTooltip();
        }}
        onClick={() => {
          cancelScheduledClose();
          if (pinned) {
            closeTooltip();
            return;
          }
          knowledgeTooltipCoordinator.claim(ownerRef.current, closeTooltip);
          setPinned(true);
          setOpen(true);
          updatePosition();
        }}
      >
        {text}
      </button>
      {open && typeof document !== 'undefined' && createPortal(
        <div
          ref={tooltipRef}
          id={tooltipId}
          className="knowledge-reference-tooltip"
          role="dialog"
          aria-labelledby={tooltipTitleId}
          data-placement={position?.placement || 'bottom'}
          style={position
            ? {
              left: position.left,
              top: position.top,
              width: position.width,
              maxHeight: position.maxHeight,
            }
            : { left: VIEWPORT_MARGIN, top: VIEWPORT_MARGIN, width: 240 }}
          onMouseEnter={cancelScheduledClose}
          onMouseLeave={scheduleClose}
          onFocusCapture={cancelScheduledClose}
          onBlurCapture={scheduleClose}
          onPointerDown={(event) => event.stopPropagation()}
          onClick={(event) => event.stopPropagation()}
          onKeyDown={(event) => {
            if (event.key !== 'Escape') return;
            closeTooltip();
            triggerRef.current?.focus();
          }}
        >
          <header className="knowledge-reference-tooltip-title">
            <strong id={tooltipTitleId}>
              {tooltipTitle}
              {preparedCitations.length > 1
                ? translateTemplate('（{count} 项）', ' ({count})', { count: preparedCitations.length })
                : ''}
            </strong>
            <button
              className="knowledge-reference-tooltip-close"
              type="button"
              aria-label={translate('关闭引用依据', 'Close source evidence')}
              onClick={() => {
                closeTooltip();
                triggerRef.current?.focus();
              }}
            >
              ×
            </button>
          </header>
          {preparedCitations.map((preparedCitation) => {
            const {
              reference,
              sourceType,
              isExact,
              hasContext,
              exactQuote,
              blocks,
            } = preparedCitation;
            const { citation } = reference;
            const scopeLabel = KNOWLEDGE_SCOPE_LABELS[normalizedScope(citation.scope)]
              || normalizedScope(citation.scope)
              || '知识资料';
            return (
              <section
                className={[
                  'knowledge-reference-source',
                  sourceType === 'skill' ? 'is-skill' : '',
                ].filter(Boolean).join(' ')}
                key={referenceIdentity(reference)}
              >
                <header className="knowledge-reference-source-heading">
                  <strong>
                    {normalizeDisplayText(
                      citation.title
                        || citation.source_id
                        || (sourceType === 'skill'
                          ? translate('业务 Skill', 'Business skill')
                          : translate('知识库资料', 'Knowledge-base source')),
                    )}
                  </strong>
                  <span className="knowledge-reference-source-meta">
                    {sourceType === 'skill' ? 'Skill' : translate('知识库', 'Knowledge base')}
                    {' · '}
                    {translate(
                      scopeLabel,
                      KNOWLEDGE_SCOPE_ENGLISH_LABELS[scopeLabel] || scopeLabel,
                    )}
                    {' · '}
                    {hasContext
                      ? translate('原文上下文', 'Source context')
                      : isExact
                        ? translate('直接依据', 'Direct evidence')
                        : translate('相关原文', 'Related source')}
                  </span>
                </header>
                <div className={[
                  'knowledge-reference-content',
                  isExact ? 'is-exact' : '',
                ].filter(Boolean).join(' ')}>
                  {isExact && exactQuote && (
                    <div className="knowledge-reference-exact-quote">
                      <span>{translate('直接依据', 'Direct evidence')}</span>
                      <mark>{normalizeDisplayText(exactQuote)}</mark>
                    </div>
                  )}
                  {hasContext && blocks.length > 0 && (
                    <span className="knowledge-reference-context-label">{translate('所在上下文', 'Context')}</span>
                  )}
                  {blocks.map((block, blockIndex) => {
                    const key = block.type + ':' + String(blockIndex) + ':' + block.text;
                    if (block.type === 'heading') {
                      return <h4 key={key}>{renderKnowledgeInline(block.inline)}</h4>;
                    }
                    if (block.type === 'bullet' || block.type === 'ordered') {
                      return (
                        <div className="knowledge-reference-bullet" key={key}>
                          <span aria-hidden="true">
                            {block.type === 'ordered' ? block.marker : '•'}
                          </span>
                          <p>{renderKnowledgeInline(block.inline)}</p>
                        </div>
                      );
                    }
                    if (block.type === 'quote') {
                      return (
                        <blockquote key={key}>{renderKnowledgeInline(block.inline)}</blockquote>
                      );
                    }
                    return <p key={key}>{renderKnowledgeInline(block.inline)}</p>;
                  })}
                </div>
              </section>
            );
          })}
        </div>,
        document.body,
      )}
    </>
  );
}

function KnowledgeLinkedText({
  text,
  target,
  citations = [],
  className = '',
  showRelatedSources = true,
}: KnowledgeLinkedTextProps) {
  const { translate, translateTemplate } = useLanguage();
  const displayText = normalizeDisplayText(text);
  const presentation = useMemo(
    () => buildKnowledgePresentation(displayText, citations, target),
    [citations, displayText, target],
  );
  const { segments, relatedReferences } = presentation;
  const visibleRelatedReferences = showRelatedSources ? relatedReferences : [];
  const relatedSourceCount = new Set(
    visibleRelatedReferences.map((reference) => citationIdentity(reference.citation)),
  ).size;

  if (!segments.some((segment) => segment.references.length) && !visibleRelatedReferences.length) {
    return <>{displayText}</>;
  }

  return (
    <span className={['knowledge-reference', className].filter(Boolean).join(' ')}>
      {segments.map((segment, index) => {
        const key = String(index) + ':' + segment.text;
        if (!segment.references.length) {
          return <Fragment key={key}>{segment.text}</Fragment>;
        }
        return (
          <KnowledgeReferenceTrigger
            key={key}
            text={segment.text}
            references={segment.references}
          />
        );
      })}
      {visibleRelatedReferences.length > 0 && (
        <>
          {' '}
          <KnowledgeReferenceTrigger
            text={relatedSourceCount > 1
              ? translateTemplate('{count} 个相关来源', '{count} related sources', { count: relatedSourceCount })
              : translate('相关来源', 'Related source')}
            matchText={displayText}
            references={visibleRelatedReferences}
            related
          />
        </>
      )}
    </span>
  );
}

export default memo(KnowledgeLinkedText);
