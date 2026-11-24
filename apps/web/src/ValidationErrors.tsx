import { TriangleAlert } from 'lucide-react';
import { Panel } from './components';
import type { AnalysisValidationError } from './types';

export function ValidationErrors({ errors = [] }: { errors?: AnalysisValidationError[] }) {
  if (!errors.length) return null;
  return <section role="alert" aria-label="Analysis validation errors">
    <Panel icon={TriangleAlert} className="validation-panel" title="Analysis validation errors" subtitle="Some analysis results failed validation. Review the details below.">
      {errors.map((failure, index) => <div className="validation-error" key={index}>
        <h3>{failure.stage.replaceAll('_', ' ')} · {failure.kind}
          {failure.model_call != null && ` · Model call ${failure.model_call}`}
        </h3>
        {failure.request_id && <p>Request: <code>{failure.request_id}</code></p>}
        <ul>{failure.issues.map((issue, issueIndex) => <li key={issueIndex}>
          <code>{issue.path.length ? issue.path.join('.') : '$'}</code>: {issue.message} <code>({issue.code})</code>
        </li>)}</ul>
        {failure.total_issues > failure.issues.length && <p>{failure.total_issues - failure.issues.length} additional errors omitted.</p>}
      </div>)}
    </Panel>
  </section>;
}
