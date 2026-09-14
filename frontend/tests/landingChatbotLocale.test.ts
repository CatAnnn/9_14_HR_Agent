import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

function source(path: string): string {
  return readFileSync(new URL(path, import.meta.url), 'utf8');
}

test('resource chat isolates history and in-flight responses when the language changes', () => {
  const chatbot = source('../src/components/LandingChatbot.tsx');
  const client = source('../src/api/client.ts');

  assert.match(chatbot, /activeRequestRef\.current\?\.abort\(\)/u);
  assert.match(chatbot, /setMessages\(\[WELCOME_MESSAGE\]\)/u);
  assert.match(chatbot, /requestLanguage === languageRef\.current/u);
  assert.match(chatbot, /api\.sendResourceChatMessage\([\s\S]*controller\.signal\)/u);
  assert.match(chatbot, /userFacingErrorMessage\(/u);
  assert.match(client, /sendResourceChatMessage: \(payload: ResourceChatRequest, signal\?: AbortSignal\)/u);
});
