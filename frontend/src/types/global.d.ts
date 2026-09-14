declare global {
  interface Window {
    __HR_AGENT_API_BASE?: string;
    __HR_AGENT_RUNTIME_CONFIG__?: {
      workflowPageGuideEnabled?: boolean | string;
      workflowPageGuideAlwaysShow?: boolean | string;
      agentIntroductionVariant?: string;
    };
  }
}

export {};
