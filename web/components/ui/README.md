# UI Primitives

This directory contains low-level, reusable UI components such as buttons, inputs, labels, and dialogs.

## Source
Most components here are based on **Shadcn UI** and **Radix UI**. They provide accessible, unstyled primitives that are then styled with Tailwind CSS.

## Usage
These components should be used to build more complex features in the parent `components/` directory.

```tsx
import { Button } from "@/components/ui/button"

export function MyComponent() {
  return <Button variant="primary">Click Me</Button>
}
```

## Customization
To change the global style of a UI component, modify its file in this directory. We follow a "Copy and Paste" philosophy for UI primitives to allow for complete customization without external library constraints.
