# Factory pattern

Centralize object creation when construction varies by configuration or caller should not depend on concrete implementations. Validate input and reject unknown variants explicitly; do not silently fall back to an unsafe/default type. Test each supported variant, invalid selectors and dependency failures. A factory is unnecessary when direct construction is clearer.
