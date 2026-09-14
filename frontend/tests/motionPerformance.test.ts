import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

test('route feedback restarts by remounting without forcing synchronous layout', () => {
  const appSource = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const styles = readFileSync(
    new URL('../src/styles/motion-polish.css', import.meta.url),
    'utf8',
  );

  assert.match(appSource, /key=\{transitionKey\} className="route-settling-indicator"/);
  assert.doesNotMatch(appSource, /offsetWidth|is-route-settling/);
  assert.match(styles, /\.route-settling-indicator\s*\{[\s\S]*?animation:\s*routeContentSettle/);
  assert.match(styles, /\.route-settling-indicator\s*\{[\s\S]*?height:\s*1px/);
  assert.match(styles, /\.route-settling-indicator\s*\{[\s\S]*?background:\s*#051c2c/);
  assert.doesNotMatch(styles, /#root\.is-route-settling/);
});

test('route loading uses one restrained centered line without a sweeping blue bar', () => {
  const styles = readFileSync(
    new URL('../src/styles/motion-polish.css', import.meta.url),
    'utf8',
  );

  assert.match(styles, /\.route-loading-track\s*\{[^}]*width:\s*64px[^}]*height:\s*1px/s);
  assert.match(styles, /--route-loading-ink:\s*#051c2c/);
  assert.match(styles, /\.route-loading-track::after\s*\{[^}]*background:\s*var\(--route-loading-ink\)/s);
  assert.match(styles, /font-family:[^;}]*-apple-system[^;}]*SF Pro Text[^;}]*PingFang SC/s);
  assert.match(styles, /animation:\s*routeLoadingBreathe 1\.8s var\(--apple-motion-ease\)/);
  assert.match(styles, /animation:\s*routeLoadingReveal var\(--apple-motion-control\) var\(--apple-motion-enter\) 120ms both/);
  assert.match(styles, /@keyframes routeLoadingBreathe/);
  assert.match(styles, /@keyframes routeLoadingReveal/);
  assert.doesNotMatch(styles, /routeLoadingSweep|translateX\(-130%\)|#007bc0/);
});

test('the canonical motion layer loads last and preserves reduced-motion preferences', () => {
  const entrySource = readFileSync(new URL('../src/main.tsx', import.meta.url), 'utf8');
  const motionStyles = readFileSync(new URL('../src/styles/motion.css', import.meta.url), 'utf8');
  const polishStyles = readFileSync(
    new URL('../src/styles/motion-polish.css', import.meta.url),
    'utf8',
  );

  const guideIndex = entrySource.indexOf("./styles/workflow-guide.css");
  const motionIndex = entrySource.indexOf("./styles/motion.css");
  const polishIndex = entrySource.indexOf("./styles/motion-polish.css");
  assert.ok(guideIndex >= 0 && guideIndex < motionIndex && motionIndex < polishIndex);

  assert.match(polishStyles, /\[role="tab"\][\s\S]*?a\[href\][\s\S]*?opacity var\(--apple-motion-fast\)/);
  assert.match(polishStyles, /#root:is\(#root\)[\s\S]*?animation:\s*none !important[\s\S]*?transition-duration:\s*\.001ms !important/);
  assert.ok(
    polishStyles.lastIndexOf('@media (prefers-reduced-motion: reduce)')
      > polishStyles.lastIndexOf('.is-viewport-reveal-visible'),
  );
  assert.doesNotMatch(`${motionStyles}\n${polishStyles}`, /transition\s*:\s*all\b/);
});

test('scroll and viewport reveal work is coalesced without forced synchronous layout', () => {
  const scrollSource = readFileSync(
    new URL('../src/components/ScrollManager.tsx', import.meta.url),
    'utf8',
  );
  const revealSource = readFileSync(
    new URL('../src/hooks/useViewportReveal.ts', import.meta.url),
    'utf8',
  );
  const journeySource = readFileSync(
    new URL('../src/hooks/useAgentJourneyProgress.ts', import.meta.url),
    'utf8',
  );
  const knowledgeSource = readFileSync(
    new URL('../src/components/KnowledgeLinkedText.tsx', import.meta.url),
    'utf8',
  );

  assert.match(scrollSource, /addEventListener\('scroll', schedulePositionSave, \{ passive: true \}\)/);
  assert.match(scrollSource, /requestAnimationFrame\(\(\) => \{[\s\S]*?writePosition\(\)/);
  assert.match(scrollSource, /previous\?\.left === left && previous\.top === top/);
  assert.match(scrollSource, /flushBeforeSameOriginNavigation/);
  assert.doesNotMatch(scrollSource, /addEventListener\('scroll', writePosition/);

  assert.doesNotMatch(revealSource, /offsetHeight|offsetWidth/);
  assert.match(revealSource, /Read layout for every target before mutating/);
  assert.match(revealSource, /prepareFrame = window\.requestAnimationFrame[\s\S]*?armFrame = window\.requestAnimationFrame/);

  assert.doesNotMatch(journeySource, /document\.addEventListener\('scroll'/);
  assert.match(journeySource, /window\.addEventListener\('scroll', schedule, \{ passive: true \}\)/);
  assert.match(knowledgeSource, /addEventListener\('scroll', reposition, \{ capture: true, passive: true \}\)/);
});

test('high-frequency visual progress and navigation underline use transforms', () => {
  const readerSource = readFileSync(new URL('../src/pages/EbookReaderPage.tsx', import.meta.url), 'utf8');
  const readerStyles = readFileSync(new URL('../src/styles/ebook-reader.css', import.meta.url), 'utf8');
  const homeStyles = readFileSync(new URL('../src/styles/home-page.css', import.meta.url), 'utf8');

  assert.match(readerSource, /'--ebook-reader-progress': progress \/ 100/);
  assert.match(readerStyles, /\.ebook-reader-progress-track i\s*\{[^}]*transform:\s*scaleX\(var\(--ebook-reader-progress, 0\)\)/s);
  assert.doesNotMatch(readerStyles, /\.ebook-reader-progress-track i\s*\{[^}]*transition:\s*width/s);
  assert.match(homeStyles, /\.home-desktop-nav > a::after\s*\{[^}]*transform:\s*scaleX\(0\)[^}]*transition:\s*transform/s);
  assert.doesNotMatch(homeStyles, /\.home-desktop-nav > a::after\s*\{[^}]*transition:[^;}]*(?:left|right)/s);
});
