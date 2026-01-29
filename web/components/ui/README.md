# UI Primitives

This directory contains low-level, reusable UI components such as cards and layout primitives.

## Source
Most components here are based on **Shadcn UI** and **Radix UI**. They provide accessible, unstyled primitives that are then styled with Tailwind CSS.

## Usage
These components should be used to build more complex features in the parent `components/` directory.

```tsx
import { Card, CardHeader, CardContent } from "@/components/ui/card"

export function MyComponent() {
  return (
    <Card>
      <CardHeader>Title</CardHeader>
      <CardContent>Body</CardContent>
    </Card>
  )
}
```

## Customization
To change the global style of a UI component, modify its file in this directory. We follow a "Copy and Paste" philosophy for UI primitives to allow for complete customization without external library constraints.
