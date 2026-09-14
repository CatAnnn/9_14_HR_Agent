export interface RouteDocumentMetadata {
  readonly title: string;
  readonly titleEnglish: string;
}

const HOME_METADATA: RouteDocumentMetadata = {
  title: '绩效反馈与对话预演',
  titleEnglish: 'Performance Feedback & Conversation Rehearsal',
};

const STATIC_ROUTE_METADATA: Readonly<Record<string, RouteDocumentMetadata>> = {
  '/': HOME_METADATA,
  '/login': {
    title: '登录 | Performance Feedback',
    titleEnglish: 'Sign in | Performance Feedback',
  },
  '/register': {
    title: '注册 | Performance Feedback',
    titleEnglish: 'Register | Performance Feedback',
  },
  '/solutions': {
    title: '解决方案 | Performance Feedback',
    titleEnglish: 'Solutions | Performance Feedback',
  },
  '/resources': {
    title: '资源中心 | Performance Feedback',
    titleEnglish: 'Resources | Performance Feedback',
  },
  '/app/introduction': {
    title: '平台介绍 | Performance Feedback',
    titleEnglish: 'Platform overview | Performance Feedback',
  },
};

const ADMIN_METADATA: RouteDocumentMetadata = {
  title: '管理平台 | Performance Feedback',
  titleEnglish: 'Administration | Performance Feedback',
};

const ADMIN_USAGE_METADATA: RouteDocumentMetadata = {
  title: '使用详情 | Performance Feedback',
  titleEnglish: 'Usage details | Performance Feedback',
};

const ADMIN_TEST_METADATA: RouteDocumentMetadata = {
  title: '测试工作流 | Performance Feedback',
  titleEnglish: 'Test workflow | Performance Feedback',
};

const WORKSPACE_METADATA: RouteDocumentMetadata = {
  title: '管理沟通工作台 | Performance Feedback',
  titleEnglish: 'Management Conversation Workspace | Performance Feedback',
};

/**
 * Returns metadata only for routes with stable titles. Dynamic detail routes set
 * their specific title in the page after resolving the requested content.
 */
export function routeDocumentMetadata(pathname: string): RouteDocumentMetadata | undefined {
  const normalizedPath = pathname.length > 1 ? pathname.replace(/\/+$/u, '') : pathname;
  const exact = STATIC_ROUTE_METADATA[normalizedPath];
  if (exact) return exact;
  if (normalizedPath.startsWith('/admin/usage/')) return ADMIN_USAGE_METADATA;
  if (normalizedPath === '/admin/test-workflow' || normalizedPath.startsWith('/admin/test-workflow/')) {
    return ADMIN_TEST_METADATA;
  }
  if (normalizedPath === '/admin') return ADMIN_METADATA;
  if (normalizedPath === '/app' || normalizedPath.startsWith('/app/')) return WORKSPACE_METADATA;
  return undefined;
}
