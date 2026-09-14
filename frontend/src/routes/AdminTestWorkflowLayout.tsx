import { Outlet } from 'react-router-dom';
import { LoadingOverlay } from '../components/LoadingOverlay';
import { RouteRedirect } from '../components/RouteLoading';
import { Toast } from '../components/Toast';
import { WorkflowProvider } from '../context/WorkflowContext';
import { useAuthStore } from '../store/authStore';
import { ADMIN_TEST_SESSION_STORAGE_KEY } from '../utils/adminTestWorkflow';

export default function AdminTestWorkflowLayout() {
  const { user } = useAuthStore();
  let hasStoredSession = false;
  try {
    hasStoredSession = Boolean(window.localStorage.getItem(ADMIN_TEST_SESSION_STORAGE_KEY));
  } catch {
    // A test workflow cannot be resumed when its local session id is inaccessible.
  }

  if (user?.role !== 'admin') return <RouteRedirect to="/" />;
  if (!hasStoredSession) {
    return <RouteRedirect to="/admin/test-workflow" />;
  }

  return (
    <WorkflowProvider
      sessionStorageKey={ADMIN_TEST_SESSION_STORAGE_KEY}
      allowSessionCreation={false}
      restorePreviousEmployeeSettings
    >
      <Outlet />
      <LoadingOverlay />
      <Toast />
    </WorkflowProvider>
  );
}
