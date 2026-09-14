type SpeechCancellationSocket = {
  readonly readyState: number;
  send(data: string): void;
};

export function sendSpeechStreamCancellation({
  activeSocket,
  currentSocket,
  streamId,
}: {
  activeSocket: SpeechCancellationSocket | null;
  currentSocket: SpeechCancellationSocket | null;
  streamId: string | null;
}): boolean {
  if (!streamId || !activeSocket || activeSocket !== currentSocket) return false;
  // WebSocket.OPEN is 1; keeping this independent of the browser also permits
  // cancellation to be tested without opening a real connection.
  if (activeSocket.readyState !== 1) return false;
  try {
    activeSocket.send(JSON.stringify({ type: 'cancel', speech_stream_id: streamId }));
    return true;
  } catch {
    // A closing connection must never prevent local playback from stopping.
    return false;
  }
}
