Review the interface as someone using it, including someone using a keyboard or a screen reader.

Accessibility:

- Controls without an accessible name, icon-only buttons without a label, images without alt text.
- State conveyed only by color or visually: a selected option without `aria-pressed` or `aria-selected`, errors not
  associated with their field.
- Semantics that break assistive technology: a click handler on a `div`, invalid nesting such as a non-`li` child of `ul`.
- Keyboard: focus not moved into a dialog or not returned, focus traps, an action only available on hover.
- Contrast: a color token or theme value too low-contrast for its surface, especially one not overridden for dark mode. Read
  the token's value.

Behavior and states:

- Missing loading, empty or error states, so a failed request looks like no data. A button not disabled during submit.
- A form that loses input, or validation that disagrees with the server.
- Layout that breaks at realistic sizes: long text, small screens, right-to-left.

Consistency:

- Hard-coded user-visible strings where the repository uses translations, or a key that is missing for one language.
- A component reimplemented when the design system already has it. Name the existing one.
