import fs from 'node:fs';
import path from 'node:path';
import ts from 'typescript';

const root = process.cwd();
const read = (relativePath) => fs.readFileSync(path.join(root, relativePath), 'utf8');

const toolkitContent = read('src/content/toolkit-content.ts');
const routeMatches = [...toolkitContent.matchAll(
  /id:\s*'([^']+)',\s*\n\s*path:\s*toolPath\('([^']+)',\s*'([^']+)'\)/g,
)];
const routeTools = routeMatches.map((match) => ({ id: match[1], category: match[2], slug: match[3] }));

if (routeTools.length !== 15) {
  throw new Error(`Expected 15 method routes, found ${routeTools.length}.`);
}
for (const tool of routeTools) {
  if (tool.id !== tool.slug) throw new Error(`Route slug mismatch for ${tool.id}: ${tool.slug}.`);
}

const configSource = read('src/content/method-article-config.ts');
const compiledConfig = ts.transpileModule(configSource, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;
const configModule = { exports: {} };
Function('exports', 'module', compiledConfig)(configModule.exports, configModule);
const configs = configModule.exports.methodArticleConfigs;
const configuredIds = Object.keys(configs);
const routeIds = routeTools.map((tool) => tool.id);

const missingConfigs = routeIds.filter((id) => !configuredIds.includes(id));
const unexpectedConfigs = configuredIds.filter((id) => !routeIds.includes(id));
if (missingConfigs.length || unexpectedConfigs.length) {
  throw new Error(`Method registry mismatch. Missing: ${missingConfigs.join(', ') || 'none'}; unexpected: ${unexpectedConfigs.join(', ') || 'none'}.`);
}

const customSources = {
  'skills-gap-analysis': 'src/components/toolkit/SkillsGapAnalysisArticle.tsx',
  'grow-model': 'src/components/toolkit/GrowModelArticle.tsx',
  'smart-goals': 'src/components/toolkit/SmartGoalsArticle.tsx',
  '360-degree-feedback': 'src/components/toolkit/ThreeSixtyFeedbackArticle.tsx',
  'leadership-self-assessment': 'src/components/toolkit/LeadershipSelfAssessmentArticle.tsx',
  'belbin-team-roles': 'src/components/toolkit/BelbinTeamRolesArticle.tsx',
  'soft-skills-assessment': 'src/components/toolkit/SoftSkillsAssessmentArticle.tsx',
  'workplace-skills-assessment': 'src/components/toolkit/WorkplaceSkillsAssessmentArticle.tsx',
};
const genericSource = read('src/pages/ToolkitPage.tsx');

for (const [methodId, config] of Object.entries(configs)) {
  const sectionIds = config.sections.map((section) => section.id);
  if (sectionIds.length === 0 || new Set(sectionIds).size !== sectionIds.length) {
    throw new Error(`${methodId} has no sections or contains duplicate section IDs.`);
  }
  const componentSource = customSources[methodId] ? read(customSources[methodId]) : genericSource;
  const missingSections = sectionIds.filter((id) => !componentSource.includes(`id="${id}"`));
  if (missingSections.length) {
    throw new Error(`${methodId} references missing rendered sections: ${missingSections.join(', ')}.`);
  }
}

const appSource = read('src/App.tsx');
if (!appSource.includes('path="/solutions/:toolkitSlug/:toolSlug"')) {
  throw new Error('The method detail route declaration is missing.');
}

const obsoleteNavigationClasses = [
  'grow-article-nav',
  'sga-toc',
  'smart-goals-directory',
  'f360-contents',
  'lsa-contents',
  'belbin-contents',
  'ssa-toc',
  'wsa-toc',
];
for (const relativePath of Object.values(customSources)) {
  const source = read(relativePath);
  const obsolete = obsoleteNavigationClasses.find((className) => source.includes(`className="${className}`));
  if (obsolete) throw new Error(`${relativePath} still renders obsolete navigation ${obsolete}.`);
}

process.stdout.write(`Validated ${routeTools.length} method routes and ${configuredIds.length} article configurations.\n`);
