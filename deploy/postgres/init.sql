-- One database per service so that stores stay physically separable, as the architecture
-- requires (Identity Vault, Social Registry, Program Store, Audit Store, ...).
-- In production each database gets its own role, credentials and network policy; this file is
-- only the shape of that separation for the evaluation deployment.
CREATE DATABASE pdp;
CREATE DATABASE audit;
CREATE DATABASE vault;
CREATE DATABASE registry;
CREATE DATABASE program;
CREATE DATABASE broker;
CREATE DATABASE eligibility;
CREATE DATABASE breakglass;
CREATE DATABASE payments;
CREATE DATABASE retention;
