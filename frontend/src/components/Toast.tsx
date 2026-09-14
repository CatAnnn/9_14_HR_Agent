import { useWorkflowFeedback } from '../context/WorkflowContext';

export function Toast() {
  const { toast } = useWorkflowFeedback();
  return <div className={`toast ${toast ? `show ${toast.type}` : ''}`} role="status">{toast?.message}</div>;
}
