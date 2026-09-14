import type { BigFivePersonality } from '../types/domain';

export type PersonalityDimension = keyof BigFivePersonality;

export interface PersonalityDimensionCopy {
  key: PersonalityDimension;
  label: string;
  low: string;
  mid: string;
  high: string;
}

export const PERSONALITY_DIMENSIONS: readonly PersonalityDimensionCopy[] = [
  { key: 'openness', label: '愿意尝试新方法', low: '更喜欢熟悉的方法', mid: '愿意试一些新方法', high: '很愿意尝试新方法' },
  { key: 'conscientiousness', label: '做事具有计划性', low: '常常边做边调整', mid: '一般会按计划做', high: '会提前计划并认真完成' },
  { key: 'extraversion', label: '主动地表达想法', low: '通常听得多、说得少', mid: '需要时会说出想法', high: '经常主动说出想法' },
  { key: 'agreeableness', label: '愿意和别人商量', low: '更愿意按自己的想法做', mid: '会和别人商量', high: '会主动听取意见，一起想办法' },
  { key: 'neuroticism', label: '面对压力保持平稳', low: '比较容易紧张或担心', mid: '压力大时会有些紧张', high: '遇到压力时比较平静' },
];

export function toPersonalitySliderValue(key: PersonalityDimension, storedValue: number) {
  return key === 'neuroticism' ? 100 - storedValue : storedValue;
}

export function fromPersonalitySliderValue(key: PersonalityDimension, sliderValue: number) {
  return key === 'neuroticism' ? 100 - sliderValue : sliderValue;
}

export function personalitySliderText(value: number, dimension: PersonalityDimensionCopy) {
  if (value < 35) return dimension.low;
  if (value > 65) return dimension.high;
  return dimension.mid;
}
