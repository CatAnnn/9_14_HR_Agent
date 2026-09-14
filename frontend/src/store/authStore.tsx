import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import { authApi } from '../api/auth';
import type { AuthUser } from '../types/auth';
import { ADMIN_TEST_SESSION_STORAGE_KEY } from '../utils/adminTestWorkflow';
import { shouldClearWorkspaceAfterAuthFailure } from '../utils/authFailure';
import { clearAllWorkflowDrafts } from '../utils/workflowDraftStorage';

type AuthContextValue = {
  initialized: boolean;
  user: AuthUser | null;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string, displayName?: string) => Promise<boolean>;
  logout: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | null>(null);
const WORKFLOW_SESSION_STORAGE_KEY = 'hr_agent_session_id';

function clearUserWorkspace() {
  for (const key of [WORKFLOW_SESSION_STORAGE_KEY, ADMIN_TEST_SESSION_STORAGE_KEY]) {
    try {
      window.localStorage.removeItem(key);
    } catch {
      // Authentication state still clears when one browser storage entry is unavailable.
    }
  }
  for (const key of [WORKFLOW_SESSION_STORAGE_KEY, ADMIN_TEST_SESSION_STORAGE_KEY]) {
    try {
      clearAllWorkflowDrafts(window.sessionStorage, key);
    } catch {
      // Do not let blocked session storage prevent logout or auth expiry cleanup.
    }
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [initialized, setInitialized] = useState(false);
  const [user, setUser] = useState<AuthUser | null>(null);

  const refresh = useCallback(async () => {
    try {
      const result = await authApi.getMe();
      setUser(result.authenticated ? result.user || null : null);
    } catch (error) {
      if (shouldClearWorkspaceAfterAuthFailure(error)) clearUserWorkspace();
      setUser(null);
    } finally {
      setInitialized(true);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    const expire = () => {
      clearUserWorkspace();
      setUser(null);
      setInitialized(true);
    };
    window.addEventListener('hr-auth-expired', expire);
    return () => window.removeEventListener('hr-auth-expired', expire);
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    const result = await authApi.login(email, password);
    if (!result.success || !result.user) {
      throw new Error('邮箱或密码错误，或账号暂不可用。');
    }
    clearUserWorkspace();
    setUser(result.user);
    setInitialized(true);
  }, []);

  const register = useCallback(async (
    email: string,
    password: string,
    displayName?: string,
  ) => {
    const result = await authApi.register(email, password, displayName);
    return Boolean(result.success);
  }, []);

  const logout = useCallback(async () => {
    try {
      await authApi.logout();
    } catch {
      // Local state must still be cleared when the server session already expired.
    } finally {
      clearUserWorkspace();
      setUser(null);
      setInitialized(true);
    }
  }, []);

  const value = useMemo(
    () => ({ initialized, user, login, register, logout }),
    [initialized, user, login, register, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuthStore() {
  const context = useContext(AuthContext);
  if (!context) throw new Error('useAuthStore must be used within AuthProvider');
  return context;
}
