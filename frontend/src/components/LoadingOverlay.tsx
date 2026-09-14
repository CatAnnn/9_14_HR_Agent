import { useWorkflowFeedback } from '../context/WorkflowContext';

export function LoadingOverlay() {
  const { loading } = useWorkflowFeedback();

  if (!loading.active) return null;

  return (
    <div
      className="workflow-busy-guard"
      role="status"
      aria-busy="true"
      aria-live="polite"
    >
      <span className="sr-only">{loading.text}</span>
    </div>
  );
}
