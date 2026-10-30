# UI Primitives

This directory contains low-level, reusable UI components:

- `card.tsx`: the `Card` component family (`Card`, `CardHeader`, `CardTitle`, `CardContent`).
- `ConfirmDialog.tsx`: one yes/no question in front of something that is not cheap to take back.
- `PickerDialog.tsx`: the searchable centred picker (`PickerSelect`), used for the language and the timezone.
- `Switch.tsx`: the house two-position switch.
- `Skeleton.tsx`: the placeholder for a region waiting for its first data (`Skeleton`, `SkeletonBar`, `SkeletonRows`). Sites choose sizes and layout only; the reveal delay, the opacity pulse, the grey and the screen-reader status live here and in `globals.css`. The rules are in "Loading states" in [docs/web-ui/WEB_UI.md](../../../docs/web-ui/WEB_UI.md).

## Source
Components here follow the **Shadcn UI** approach: unstyled primitives copied into the codebase and then styled with Tailwind CSS.

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
