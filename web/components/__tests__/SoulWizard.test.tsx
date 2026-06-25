// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import SoulWizard from '../SoulWizard';

describe('SoulWizard', () => {
  const mockOnComplete = jest.fn();
  const mockOnClose = jest.fn();

  beforeEach(() => {
    jest.clearAllMocks();
  });

  test('renders step 1 (Core Truths) by default', () => {
    render(
      <SoulWizard 
        isOpen={true} 
        onClose={mockOnClose} 
        onComplete={mockOnComplete} 
        username="testuser" 
      />
    );
    expect(screen.getByText('Core Truths')).toBeInTheDocument();
  });

  test('shows validation error if content is too short', () => {
    render(
      <SoulWizard 
        isOpen={true} 
        onClose={mockOnClose} 
        onComplete={mockOnComplete} 
        username="testuser" 
      />
    );
    
    const nextButton = screen.getByText('Next Step');
    fireEvent.click(nextButton);
    
    expect(screen.getByText(/Please provide more detail/i)).toBeInTheDocument();
  });

  test('transitions through all 4 steps and completes', () => {
    render(
      <SoulWizard 
        isOpen={true} 
        onClose={mockOnClose} 
        onComplete={mockOnComplete} 
        username="testuser" 
      />
    );

    // Step 1: Core Truths
    const textarea = screen.getByPlaceholderText(/- I am a helper/i);
    fireEvent.change(textarea, { target: { value: 'This is a test core truth for the wizard.' } });
    fireEvent.click(screen.getByText('Next Step'));

    // Step 2: Boundaries
    expect(screen.getByText('Boundaries')).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText(/- I will never/i), { target: { value: 'This is a test boundary for the wizard.' } });
    fireEvent.click(screen.getByText('Next Step'));

    // Step 3: Vibe
    expect(screen.getByText('Vibe')).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText(/- Speak in a technical tone/i), { target: { value: 'This is a test vibe for the wizard.' } });
    fireEvent.click(screen.getByText('Next Step'));

    // Step 4: Continuity
    expect(screen.getByText('Continuity')).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText(/- Learn from codebase/i), { target: { value: 'This is a test continuity for the wizard.' } });
    fireEvent.click(screen.getByText('Complete Soul'));

    expect(mockOnComplete).toHaveBeenCalledWith(expect.stringContaining('# SOUL of testuser'));
    expect(mockOnComplete).toHaveBeenCalledWith(expect.stringContaining('## 1. Core Truths'));
  });
});
