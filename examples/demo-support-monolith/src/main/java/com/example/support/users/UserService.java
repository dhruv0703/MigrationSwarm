package com.example.support.users;

import org.springframework.stereotype.Service;

@Service
public class UserService implements UserDirectory {
    private final SupportUserRepository repository;

    public UserService(SupportUserRepository repository) { this.repository = repository; }

    @Override
    public SupportUser requireActive(String externalId) {
        return repository.findByExternalId(externalId)
                .filter(SupportUser::isActive)
                .orElseGet(() -> repository.save(new SupportUser(externalId)));
    }
}
