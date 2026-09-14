class Pcm16Processor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.targetSampleRate = options.processorOptions?.targetSampleRate || 16000;
    this.sourceAccumulator = 0;
    this.sourceCount = 0;
    this.rateAccumulator = 0;
    this.chunkSize = Math.max(1, Math.round(this.targetSampleRate / 10));
    this.output = new Int16Array(this.chunkSize);
    this.outputOffset = 0;
    this.port.onmessage = (event) => {
      if (event.data?.type !== 'flush') return;
      this.flush();
      this.port.postMessage({ type: 'flushed' });
    };
  }

  emitOutput(length) {
    if (length <= 0) return;
    const pcm = length === this.output.length
      ? this.output.buffer
      : this.output.slice(0, length).buffer;
    this.port.postMessage({ type: 'pcm', pcm }, [pcm]);
    this.output = new Int16Array(this.chunkSize);
    this.outputOffset = 0;
  }

  flush() {
    this.emitOutput(this.outputOffset);
  }

  pushSample(sample) {
    const clamped = Math.max(-1, Math.min(1, sample));
    this.output[this.outputOffset] = clamped < 0
      ? Math.round(clamped * 0x8000)
      : Math.round(clamped * 0x7fff);
    this.outputOffset += 1;
    if (this.outputOffset < this.output.length) return;

    this.emitOutput(this.output.length);
  }

  process(inputs, outputs) {
    const input = inputs[0]?.[0];
    const output = outputs[0]?.[0];
    if (output) output.fill(0);
    if (!input?.length) return true;

    if (sampleRate <= this.targetSampleRate) {
      for (let index = 0; index < input.length; index += 1) {
        this.pushSample(input[index]);
      }
      return true;
    }

    for (let index = 0; index < input.length; index += 1) {
      this.sourceAccumulator += input[index];
      this.sourceCount += 1;
      this.rateAccumulator += this.targetSampleRate;
      if (this.rateAccumulator < sampleRate) continue;

      this.rateAccumulator -= sampleRate;
      this.pushSample(this.sourceAccumulator / this.sourceCount);
      this.sourceAccumulator = 0;
      this.sourceCount = 0;
    }
    return true;
  }
}

registerProcessor('pcm16-processor', Pcm16Processor);
