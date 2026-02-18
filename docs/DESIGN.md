# VAF Web UI Design System

This document outlines the design tokens and component styles used in the Veyllo Agent Framework Web UI to ensure consistency. **AI assistants modifying the UI MUST follow these guidelines.**

---

## 1. Core Design Philosophy

- **Light Mode Only**: The entire UI uses a clean, light design
- **Minimal Color Palette**: Primarily grays with subtle accents
- **No Dark/Zinc Colors**: Never use `zinc`, `slate` for backgrounds in the main UI
- **Consistent Rounding**: Use `rounded-xl` or `rounded-2xl` for modern look
- **Subtle Shadows**: Prefer `shadow-sm` for cards, `shadow-xl` for floating elements

---

## 2. Typography

| Element | Classes |
|---------|---------|
| **Font Family** | `font-sans` (System stack / Inter) |
| **Page Headings** | `text-xl font-bold text-gray-800` or `text-2xl font-bold text-gray-900` |
| **Section Headings** | `text-lg font-semibold text-gray-900` |
| **Subheadings** | `text-sm font-medium text-gray-700` |
| **Body Text** | `text-sm text-gray-600` or `text-sm text-gray-700` |
| **Descriptions** | `text-sm text-gray-500` |
| **Meta/Helper Text** | `text-xs text-gray-400` or `text-xs text-gray-500` |
| **Monospace/Code** | `font-mono text-xs` |

---

## 3. Color Palette

### 3.1 Backgrounds

| Usage | Class | Notes |
|-------|-------|-------|
| **Main App Background** | `bg-gray-50` | The outermost container |
| **Panels / Cards** | `bg-white` | All floating panels, modals, cards |
| **Secondary/Muted Areas** | `bg-gray-50` | Section backgrounds within cards |
| **Hover States** | `hover:bg-gray-100` | For interactive elements |
| **User Message Bubble** | `bg-gray-800 text-white` | Dark bubble for user input |
| **Bot Message Bubble** | `bg-white border border-gray-200` | Light bubble for assistant |

### 3.2 Text Colors

| Usage | Class |
|-------|-------|
| **Primary Text** | `text-gray-900` |
| **Secondary Text** | `text-gray-700` |
| **Muted/Description** | `text-gray-500` |
| **Placeholder/Disabled** | `text-gray-400` |
| **On Dark Background** | `text-white` |

### 3.3 Borders

| Usage | Class |
|-------|-------|
| **Default Border** | `border-gray-200` |
| **Subtle Separator** | `border-gray-100` |
| **Focus Ring** | `focus:ring-2 focus:ring-gray-400` or `focus:ring-blue-500/20` |

### 3.4 Status Colors

| Status | Background | Text | Border |
|--------|------------|------|--------|
| **Success/Connected** | `bg-green-100` | `text-green-700` | `border-green-500` |
| **Error/Failed** | `bg-red-100` | `text-red-600` | `border-red-500` |
| **Warning** | `bg-yellow-100` | `text-yellow-700` | `border-yellow-500` |
| **Info/Running** | `bg-blue-100` | `text-blue-700` | `border-blue-500` |
| **Neutral/Idle** | `bg-gray-100` | `text-gray-500` | `border-gray-300` |

---

## 4. Components

### 4.1 Buttons

#### Primary Button (Main Action)
```
bg-gray-900 hover:bg-gray-800 text-white font-medium px-4 py-2 rounded-lg transition-colors
```

#### Secondary Button
```
bg-gray-100 hover:bg-gray-200 text-gray-700 font-medium px-4 py-2 rounded-lg transition-colors
```

#### Danger Button
```
bg-red-500 hover:bg-red-600 text-white font-medium px-4 py-2 rounded-lg transition-colors
```

#### Ghost/Icon Button
```
p-2 hover:bg-gray-100 rounded-lg transition-colors text-gray-500 hover:text-gray-700
```

#### Disabled State
```
bg-gray-100 text-gray-400 cursor-not-allowed
```

### 4.2 Cards / Panels

#### Standard Card
```jsx
<div className="bg-white rounded-xl border border-gray-200 shadow-sm p-4">
  {/* content */}
</div>
```

#### Elevated Card (Floating/Modal)
```jsx
<div className="bg-white rounded-2xl border border-gray-200 shadow-xl p-6">
  {/* content */}
</div>
```

#### List Item Card
```jsx
<div className="bg-white rounded-xl border border-gray-200 p-4 hover:shadow-sm transition-shadow">
  {/* content */}
</div>
```

#### Muted/Inactive Card
```jsx
<div className="bg-gray-50 rounded-xl border border-gray-200 p-4">
  {/* content */}
</div>
```

### 4.3 Form Inputs

#### Text Input
```jsx
<input className="w-full px-4 py-3 rounded-xl bg-white border border-gray-300 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:border-transparent" />
```

#### Select
```jsx
<select className="w-full h-10 px-4 bg-white border border-gray-200 rounded-lg text-sm text-gray-700 focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500">
```

#### Toggle Switch
Use the dark accent color (same as primary buttons and settings nav) for the "on" state.
```jsx
<button className={cn(
  "relative w-11 h-6 rounded-full transition-colors",
  enabled ? "bg-gray-800" : "bg-gray-300"
)}>
  <div className={cn(
    "absolute top-1 w-4 h-4 rounded-full bg-white shadow transition-transform",
    enabled ? "translate-x-6" : "translate-x-1"
  )} />
</button>
```

### 4.4 Modals / Dialogs

#### Full-Screen Modal Structure
```jsx
{/* Backdrop */}
<div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 backdrop-blur-sm">
  {/* Modal Container */}
  <div className="bg-white rounded-2xl shadow-2xl w-full max-w-2xl mx-4 overflow-hidden border border-gray-200">
    {/* Header */}
    <div className="flex items-center justify-between p-6 border-b border-gray-200 bg-gray-50">
      <div className="flex items-center gap-3">
        <div className="w-10 h-10 rounded-xl bg-gray-900 flex items-center justify-center">
          <Icon className="w-5 h-5 text-white" />
        </div>
        <div>
          <h2 className="text-xl font-bold text-gray-900">Modal Title</h2>
          <p className="text-sm text-gray-500">Subtitle</p>
        </div>
      </div>
      <button className="p-2 hover:bg-gray-200 rounded-lg transition-colors">
        <X className="w-5 h-5 text-gray-500" />
      </button>
    </div>

    {/* Content */}
    <div className="p-6">
      {/* ... */}
    </div>

    {/* Footer */}
    <div className="flex items-center justify-between p-6 border-t border-gray-200 bg-gray-50">
      <button className="text-gray-600 hover:bg-gray-200 px-4 py-2 rounded-lg">
        Cancel
      </button>
      <button className="bg-gray-900 hover:bg-gray-800 text-white px-6 py-2 rounded-lg font-medium">
        Confirm
      </button>
    </div>
  </div>
</div>
```

### 4.5 Status Badges

```jsx
{/* Connected/Success */}
<span className="text-xs px-2 py-0.5 rounded-full bg-green-100 text-green-700">
  Connected
</span>

{/* Error */}
<span className="text-xs px-2 py-0.5 rounded-full bg-red-100 text-red-600">
  Error
</span>

{/* Pending/Loading */}
<span className="text-xs px-2 py-0.5 rounded-full bg-yellow-100 text-yellow-700">
  Checking...
</span>

{/* Neutral/Disabled */}
<span className="text-xs px-2 py-0.5 rounded-full bg-gray-100 text-gray-500">
  Coming Soon
</span>
```

### 4.6 Icon Containers

#### Active/Configured State
```jsx
<div className="w-10 h-10 rounded-xl bg-gray-900 text-white flex items-center justify-center">
  <Icon className="w-5 h-5" />
</div>
```

#### Inactive State
```jsx
<div className="w-10 h-10 rounded-xl bg-gray-200 text-gray-500 flex items-center justify-center">
  <Icon className="w-5 h-5" />
</div>
```

#### With Custom Color (for branded services)
```jsx
<div className={cn(
  "w-10 h-10 rounded-xl flex items-center justify-center text-white",
  app.iconColor || "bg-gray-900"  // e.g., "bg-indigo-600" for Discord
)}>
  <Icon className="w-5 h-5" />
</div>
```

---

## 5. Layout Patterns

### 5.1 Sidebar
```jsx
<aside className="flex flex-col h-full bg-white border-r border-gray-200 w-16 hover:w-72 transition-all duration-300 shadow-lg overflow-hidden">
  {/* Sidebar content */}
</aside>
```

### 5.2 Settings Panel Structure
```jsx
<div className="space-y-6">
  {/* Section Header */}
  <div>
    <h3 className="text-lg font-semibold text-gray-900 mb-1">Section Title</h3>
    <p className="text-sm text-gray-500">Section description text.</p>
  </div>

  {/* Category */}
  <div className="space-y-3">
    <div>
      <h4 className="text-sm font-medium text-gray-700">Category Label</h4>
      <p className="text-xs text-gray-400">Category description</p>
    </div>

    {/* Items */}
    <div className="space-y-2">
      {/* Item cards go here */}
    </div>
  </div>
</div>
```

### 5.3 Progress Steps
```jsx
<div className="flex items-center gap-2">
  {steps.map((step, idx) => (
    <React.Fragment key={step.id}>
      <div className={cn(
        "w-8 h-8 rounded-full flex items-center justify-center text-sm font-medium",
        idx < currentStep ? "bg-green-500 text-white" :
        idx === currentStep ? "bg-gray-900 text-white" :
        "bg-gray-200 text-gray-500"
      )}>
        {idx < currentStep ? <Check className="w-4 h-4" /> : idx + 1}
      </div>
      {idx < steps.length - 1 && (
        <div className={cn(
          "flex-1 h-1 rounded-full",
          idx < currentStep ? "bg-green-500" : "bg-gray-200"
        )} />
      )}
    </React.Fragment>
  ))}
</div>
```

---

## 6. Spacing Guidelines

| Usage | Class |
|-------|-------|
| **Container Padding** | `p-4` or `p-6` |
| **Card Padding** | `p-4` |
| **Modal Padding** | `p-6` |
| **Section Gap** | `space-y-6` |
| **Item Gap** | `space-y-2` or `space-y-3` |
| **Inline Element Gap** | `gap-2` or `gap-3` |
| **Icon + Text Gap** | `gap-2` or `gap-3` |

---

## 7. Animation & Transitions

```jsx
// Standard transition
className="transition-colors"          // For color changes
className="transition-all duration-200" // For size/transform changes
className="transition-transform"        // For transforms only

// Hover lift effect
className="hover:shadow-md transition-shadow"

// Modal entrance
className="animate-in fade-in zoom-in-95 duration-200"
```

---

## 8. Z-Index Scale

| Layer | Z-Index | Usage |
|-------|---------|-------|
| **Base Content** | `z-0` to `z-10` | Normal page content |
| **Sidebar** | `z-20` | Left navigation |
| **Dropdowns** | `z-30` | Dropdown menus |
| **Sticky Headers** | `z-40` | Fixed headers |
| **Modals** | `z-50` | Primary modals |
| **Nested Modals** | `z-[60]` | Modals over modals (e.g., Discord Wizard) |
| **Tooltips/Popovers** | `z-[9999]` | Always on top (e.g., autocomplete) |

---

## 9. DO NOT Use

These patterns are explicitly forbidden to maintain design consistency:

| Forbidden | Use Instead |
|-----------|-------------|
| `bg-zinc-*` | `bg-gray-*` |
| `bg-slate-*` | `bg-gray-*` |
| `text-white` on light backgrounds | `text-gray-900` |
| `bg-indigo-600` for primary buttons | `bg-gray-900` |
| `bg-blue-600` for primary actions | `bg-gray-900` |
| Dark mode patterns | Light mode only |
| `rounded-sm` or `rounded` | `rounded-lg` or `rounded-xl` |
| Hard shadows (`shadow-2xl` on cards) | `shadow-sm` or `shadow-md` |

---

## 10. Responsive Considerations

- Use `max-w-*` classes for content width limits
- Modal max widths: `max-w-md`, `max-w-lg`, `max-w-2xl`, `max-w-4xl`
- Grid breakpoints: `sm:`, `md:`, `lg:`, `xl:`, `2xl:`
- Always include mobile-friendly touch targets (min 44x44px for buttons)

---

## 11. Accessibility

- Ensure sufficient color contrast (text on backgrounds)
- Use `focus:ring-*` for keyboard navigation visibility
- Include `title` attributes on icon-only buttons
- Use semantic HTML elements where possible
