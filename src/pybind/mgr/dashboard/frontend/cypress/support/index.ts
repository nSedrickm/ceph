import '@applitools/eyes-cypress/commands';

import './commands';
import 'cypress-axe';


afterEach(() => {
  cy.visit('#/403');
});

Cypress.on('uncaught:exception', (err: Error) => {
  return !err.message.includes('ResizeObserver loop limit exceeded');
});
