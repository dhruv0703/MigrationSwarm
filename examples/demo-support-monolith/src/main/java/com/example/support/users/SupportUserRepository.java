package com.example.support.users;

import java.util.Optional;
import org.springframework.data.jpa.repository.JpaRepository;

public interface SupportUserRepository extends JpaRepository<SupportUser, Long> {
    Optional<SupportUser> findByExternalId(String externalId);
}
