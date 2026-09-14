import { Outlet } from 'react-router-dom';
import { LoadingOverlay } from '../components/LoadingOverlay';
import { Toast } from '../components/Toast';
import { WorkflowPageGuideProvider } from '../components/WorkflowPageGuide';
import { WorkflowProvider } from '../context/WorkflowContext';
import { useAuthStore } from '../store/authStore';
import { shouldRestorePreviousEmployeeSettings } from '../utils/employeeSetupSettings';

export default function WorkflowLayout() {
  const { user } = useAuthStore();

  return (
    <WorkflowProvider
      key={user?.email || 'anonymous'}
      restorePreviousEmployeeSettings={shouldRestorePreviousEmployeeSettings(user?.role)}
    >
      <WorkflowPageGuideProvider>
        <Outlet />
        <LoadingOverlay />
        <Toast />
      </WorkflowPageGuideProvider>
    </WorkflowProvider>
  );
}
