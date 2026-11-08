import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ReasoningDetails } from './Investigation';

describe('diagnosis reasoning', () => {
  it('renders review failures with the retained diagnosis explanation', () => {
    render(<ReasoningDetails summary={{ mode: 'structured_review', prompt_version: 'structured-review-v1', review_status: 'failed', review_reasons: ['causal_gap'], review_failure: 'timeout', model_calls: 2 }} />);
    expect(screen.getByText('Evidence review')).toBeInTheDocument();
    expect(screen.getByText(/causal gap/)).toBeInTheDocument();
    expect(screen.getByText(/latest valid diagnosis is retained/)).toBeInTheDocument();
  });
  it('supports historical reports without reasoning metadata', () => {
    const { container } = render(<ReasoningDetails />);
    expect(container).toBeEmptyDOMElement();
  });
});
